"""Pinned Hugging Face identities of the Whisper models BabelFishR installs.

GENERATED DATA - refresh with ``scripts/refresh_model_pins.py`` (it asks the
real Hub API for each repository's current commit and the per-file sha256 at
that commit, cross-checks every Git blob id, and rewrites this file). Do not
type values in by hand: a digest that was not read from the Hub is worse than
none.

Each entry: ``repo`` (Hub repository), ``revision`` (the full 40-hex commit
every file is fetched from - never a branch name, so a repair cannot mix
files from two revisions) and ``files`` ({filename: {"sha256", "size",
"git_oid"}}). An entry whose ``revision`` is ``None`` or whose ``files`` is
empty is *unpinned*: its identity cannot be established, so it is refused for
installation and never reported as verified (model_integrity.pin_for).

State of this file: the revisions below were supplied by the project's
director, who checked them against the published Hub (2026-11); this build
could not reach huggingface.co to read them itself, nor the per-file sha256,
so ``files`` is still empty and every model is refused (fail closed) until
``python scripts/refresh_model_pins.py --revision pinned`` fills in the
digests at exactly these commits from a machine with Hub access.
The Turbo repository moved: the Hub redirects
mobiuslabsgmbh/faster-whisper-large-v3-turbo (faster-whisper's own name
table) to dropbox-dash/faster-whisper-large-v3-turbo. The served repository
is pinned; the old id stays a ``repo_aliases`` entry so install records made
under it are still recognised (same commits, same files).
"""

PINS = {
    'tiny': {'repo': 'Systran/faster-whisper-tiny',
             'revision': 'd90ca5fe260221311c53c58e660288d3deb8d356', 'files': {}},
    'base': {'repo': 'Systran/faster-whisper-base',
             'revision': 'ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66', 'files': {}},
    'small': {'repo': 'Systran/faster-whisper-small',
              'revision': '536b0662742c02347bc0e980a01041f333bce120', 'files': {}},
    'medium': {'repo': 'Systran/faster-whisper-medium',
               'revision': '08e178d48790749d25932bbc082711ddcfdfbc4f', 'files': {}},
    'large-v3': {'repo': 'Systran/faster-whisper-large-v3',
                 'revision': 'edaa852ec7e145841d8ffdb056a99866b5f0a478', 'files': {}},
    'large-v3-turbo': {'repo': 'dropbox-dash/faster-whisper-large-v3-turbo',
                       'repo_aliases': ['mobiuslabsgmbh/faster-whisper-large-v3-turbo'],
                       'revision': '0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf',
                       'files': {}},
}
