"""An installed model or pack whose smoke test did not run is "not checked
yet", never "not available".

The Field check dialog opens with smoke tests off. On a prepared computer
it used to say "Partly ready - recording works ... Transcription and
translation are not available yet ... Record Only mode is recommended",
the alpha-3 wording the toolbar badge had already stopped using. The CLI
summary printed "transcribe : NO". Wording only: which branch a report
lands in (field_ready, field_ready_unknown, can_record) is unchanged.
"""

from __future__ import annotations

import pytest

from babelfishr.readiness import Check, CheckStatus, ReadinessReport

from test_alpha4_operator_feedback import mock_app, pump_until, qt_app  # noqa: F401

BASE = ("Audio backend", "Recording directory writable")


def report(asr_present=True, asr_smoke=CheckStatus.SKIP,
           mt_present=True, mt_smoke=CheckStatus.SKIP) -> ReadinessReport:
    r = ReadinessReport()
    for name in BASE:
        r.add(Check(name, CheckStatus.PASS, ""))
    r.add(Check("Local ASR model present",
                CheckStatus.PASS if asr_present else CheckStatus.FAIL, ""))
    r.add(Check("Installed translation paths",
                CheckStatus.PASS if mt_present else CheckStatus.FAIL, ""))
    r.add(Check("Local transcription smoke test", asr_smoke, ""))
    r.add(Check("Local translation smoke test", mt_smoke, ""))
    return r


@pytest.mark.parametrize("kwargs,transcribe,translate", [
    (dict(), "not checked yet", "not checked yet"),
    (dict(asr_smoke=CheckStatus.PASS), "yes", "not checked yet"),
    (dict(asr_smoke=CheckStatus.PASS, mt_smoke=CheckStatus.PASS), "yes", "yes"),
    (dict(asr_present=False), "NO", "not checked yet"),
    (dict(asr_smoke=CheckStatus.FAIL, mt_present=False), "NO", "NO"),
])
def test_stage_words(kwargs, transcribe, translate):
    r = report(**kwargs)
    assert r.stage_word("transcribe") == transcribe
    assert r.stage_word("translate") == translate


def test_the_cli_summary_does_not_call_untested_unavailable():
    r = report()
    assert r.field_ready_unknown and not r.field_ready  # decision unchanged
    text = r.summary()
    assert "transcribe : not checked yet" in text
    assert "translate  : not checked yet" in text
    assert "PREPARED, NOT CHECKED YET" in text
    assert "NOT fully ready" not in text


def test_the_cli_summary_still_says_no_when_missing():
    text = report(asr_present=False, asr_smoke=CheckStatus.SKIP,
                  mt_present=False).summary()
    assert "transcribe : NO" in text and "translate  : NO" in text
    assert "NOT fully ready" in text


def _dialog(qt_app, config, store):  # noqa: F811
    from babelfishr.ui.readiness_dialog import ReadinessDialog

    dialog = ReadinessDialog(mock_app(config, store))
    assert pump_until(qt_app, lambda: dialog.recheck_button.isEnabled(), 30.0)
    return dialog


def test_the_dialog_says_not_checked_yet_for_an_untested_prepared_computer(
        qt_app, config, store):  # noqa: F811
    dialog = _dialog(qt_app, config, store)
    dialog._render(report())
    assert dialog.headline.text() == "Prepared - not checked yet"
    assert "not available" not in dialog.subtitle.text()
    assert "Record Only" not in dialog.subtitle.text()
    assert "Re-check" in dialog.subtitle.text()
    dialog.close()


def test_the_dialog_names_each_stage_when_partly_ready(qt_app, config, store):  # noqa: F811
    dialog = _dialog(qt_app, config, store)
    dialog._render(report(asr_present=False))
    assert dialog.headline.text() == "Partly ready - recording works"
    subtitle = dialog.subtitle.text()
    assert "Transcription: not available yet" in subtitle
    assert "translation: installed, not checked yet" in subtitle
    dialog.close()


def test_the_badge_detail_names_each_stage(qt_app, config, store):  # noqa: F811
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(mock_app(config, store))
    assert pump_until(qt_app, lambda: window._readiness is not None, 30.0)
    window._render_readiness(report(asr_present=False))
    detail = window.ready_badge.accessibleDescription()
    assert "transcription not available yet" in detail
    assert "translation installed, not checked yet" in detail
    assert "not the operating mode" in detail
    window.close()
