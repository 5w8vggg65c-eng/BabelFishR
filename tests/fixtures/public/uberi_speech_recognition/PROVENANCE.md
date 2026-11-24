# french.aiff (public test fixture)

- **Source:** https://github.com/Uberi/speech_recognition/blob/master/examples/french.aiff
- **Pinned commit:** `d2de540df50ce609a2df4910fac3961a0eb1ed8e`
  (raw: `https://raw.githubusercontent.com/Uberi/speech_recognition/d2de540df50ce609a2df4910fac3961a0eb1ed8e/examples/french.aiff`)
- **Licence:** the repository's BSD-3-Clause licence, reproduced unmodified in
  `LICENSE.txt` beside this file (Copyright (c) 2014-2016, Anthony Zhang). No
  separate terms for the example audio were found in that repository.
- **File:** unmodified. SHA-256
  `2bdb86e8d511a6f8e784188edc1bea6a504c703ab8ac5826246fece8b7e44582`;
  mono, 16-bit big-endian PCM, 44 100 Hz, 111 695 frames (2.53 s).
- **Why it is here:** on the actual Mac (runs 28 and 30) this clean, evenly
  loud French speech was classified as a steady TONE and never reached speech
  recognition. `tests/test_tone_vs_voiced_speech.py` replays it.
- **The field replay's WAV** was made from it by byte-swapping to
  little-endian, `audioop.ratecv` to 48 000 Hz and adding 1 s of digital
  silence at each end (217 572 frames, SHA-256
  `697511379fe74689ec6d0e80d2b5d75da1b636ddd3d92c9834337b9f1774536d`). The
  test does the same with numpy (`audioop` and `aifc` are gone in Python
  3.13); the detector's verdict and measurements are identical either way.
