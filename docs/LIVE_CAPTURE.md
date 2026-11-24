# Live capture: what starts and ends a recording

## Arm once

Start monitoring once. Each recording then starts and ends by itself:

- **Starts** when the input's audio level rises above the activity threshold
  for two 20 ms frames (`detector.threshold_dbfs` in fixed mode, or the
  tracked noise floor + `open_margin_db` in auto mode).
- **Ends** when the level stays below the close threshold for
  `detector.hang_time` (0.8 s by default).
- **Keeps** `pre_roll` before the start and `post_roll` after the last active
  frame, so soft opening and closing syllables stay in the recording. New
  installs use 0.5 s for both (previously 0.3 / 0.2). On synthetic speech
  through a −20 dBFS gate, 0.3 s kept 60% of a soft 0.25 s opening syllable
  and 0.2 s kept 35% of a soft 0.4 s closing syllable; 0.5 s kept all of both.
  A settings file that already holds values (Eric's has 0.3 / 0.2) is read as
  written; change them there to adopt 0.5 / 0.5.
- **Long reception** is handed to speech recognition in parts of about
  `segment_seconds` (15 s) while it continues, cut at the quietest frame of
  the last 3 s, so text arrives before Stop. The parts join sample for
  sample. Text for each part follows after its own processing time; 15 s is
  the part length, not a guaranteed time to transcript.
- **Squelch-tail trimming** still applies at the end of an ordinary burst
  (up to `max_tail_trim`). It is not applied at Stop to a recording already
  handed on in parts.

## Receiver status versus audio activity

No input BabelFishR supports today reports the receiver's own squelch,
carrier or RX state: the SDR++ connection (rigctl) sets and reads frequency
and mode only, and an audio interface carries audio only. So the window says
what it actually knows:

| Window shows | Meaning |
|---|---|
| `Armed · Listening` | Monitoring is on; waiting for the audio level to open the gate. |
| `Audio active (est.)` | The audio level has opened the gate. Estimated from the level, not reported by the receiver: noise loud enough looks the same. |

## Was it speech?

Each transcript is checked locally, with no download and no language list:

- the share and seconds of speech the bundled Silero voice detector hears;
- Whisper's own no-speech estimate;
- the shape of the text itself. Two narrow structural shapes are a
  recognition loop ("severe"): one non-digit character repeated unbroken 12+
  times and making up 80%+ of the text, or a line of 12+ characters made of
  at most four distinct symbols with no letter or digit anywhere (the shape
  of a real field loop: 56 brackets/symbols, three distinct, each spaced).
  Punctuation, number formatting and repeated digits ("NO!!!", "S.O.S.",
  "1, 2, 3", "00000000") are never severe. The same words over and over,
  loop-like compression, or short text with no letters is "repeated" - a
  real call ("mayday mayday mayday", a callsign or number) can be that too.

A severe loop, or two no-speech signals agreeing (including repeated words
with no-speech evidence), holds the translation and marks the message for
review. One doubtful signal, or "repeated" text on its own, marks it for
review and it is still translated. Engine confidence alone never clears a
transcript: a looping line has scored 0.93. The transcript and the original
audio are always kept. **Retry as speech in…** re-runs a message in a
language the operator chooses.

## Still to verify on hardware

Not verified here (no SDR, receiver or suitable cables in the development
environment; synthetic and public fixtures only):

- the gate, rolls and segmenting on real receiver audio, including real
  squelch crashes and squelch-tail-eliminating radios;
- whether trailing fricatives are ever trimmed as squelch on real audio;
- any receiver that could supply real RX/squelch status (none is supported
  or claimed);
- the voice detector's verdicts on real narrowband radio speech.
