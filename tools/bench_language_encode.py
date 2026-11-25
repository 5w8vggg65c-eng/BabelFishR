"""NATIVE BENCH: does detecting the language on the decode window change anything?

    python tools/bench_language_encode.py --model-dir PATH_TO_large-v3 a.wav b.wav ...

For each WAV (your own recordings; nothing leaves the machine) this runs the
real local faster-whisper engine twice with the same model:

* stock - faster-whisper's own automatic detection, which for a clip under
  30 s encodes the first window twice (detection includes one final 10 ms
  frame that decoding does not);
* shared - FasterWhisperEngine(shared_language_encode=True): the language is
  identified from the encoder output of exactly the window that is decoded,
  so the encoder runs once.

It prints figures only - durations, languages, probabilities, seconds and
whether the two transcripts and their segment timestamps are identical -
never the words. (In the application the same switch is the setting
``[asr] shared_language_encode``; the default is false.) The shared path
is off in the application until a run like this over representative traffic
shows the same language decisions (and therefore the same transcripts) at a
useful saving. Order is alternated per file so warm caches favour neither.
A transcript that differs only on a clip whose decode climbed the temperature
ladder (the app log's "highest temperature used" above 0) may be sampling,
not the language: run the stock path twice on that clip to see.
"""

from __future__ import annotations

import argparse
import sys
import time


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-dir", required=True,
                        help="a prepared model folder, e.g. .../models/large-v3")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("wavs", nargs="+")
    args = parser.parse_args(argv)

    from babelfishr.audio.wavefile import read_wav
    from babelfishr.providers.whisper_local import FasterWhisperEngine

    stock = FasterWhisperEngine(model_path=args.model_dir, beam_size=args.beam_size)
    stock.preload()
    shared = FasterWhisperEngine(model_path=args.model_dir, beam_size=args.beam_size,
                                 shared_language_encode=True)
    shared._model = stock._model           # one model in memory, as in the app

    def run(engine, audio, rate):
        began = time.monotonic()
        result = engine.transcribe(audio, rate)
        return result, time.monotonic() - began

    same_language = same_text = same_times = 0
    totals = [0.0, 0.0]
    print("file\tseconds\tstock lang (p)\tshared lang (p)\tstock s\tshared s"
          "\tsame text\tsame times")
    for index, path in enumerate(args.wavs):
        audio, rate = read_wav(path)
        order = [(0, stock), (1, shared)]
        if index % 2:
            order.reverse()
        outcome = {}
        for slot, engine in order:
            outcome[slot] = run(engine, audio, rate)
        (a, ta), (b, tb) = outcome[0], outcome[1]
        totals[0] += ta
        totals[1] += tb
        same_language += a.language == b.language
        same_text += a.text == b.text
        times = ([(x.start, x.end) for x in a.segments]
                 == [(x.start, x.end) for x in b.segments])
        same_times += times
        print(f"{index + 1}\t{audio.size / rate:.1f}\t{a.language} ({a.language_confidence})"
              f"\t{b.language} ({b.language_confidence})\t{ta:.2f}\t{tb:.2f}"
              f"\t{'yes' if a.text == b.text else 'NO'}\t{'yes' if times else 'NO'}")
    n = len(args.wavs)
    print(f"\nsame language {same_language}/{n}, same transcript {same_text}/{n}, "
          f"same timestamps {same_times}/{n}, "
          f"total {totals[0]:.1f} s stock vs {totals[1]:.1f} s shared")
    return 0 if same_language == same_text == same_times == n else 1


if __name__ == "__main__":
    sys.exit(main())
