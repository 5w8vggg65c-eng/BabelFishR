"""Headless checks on the Qt front-end."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from babelfishr.models import ProcessingState, Transmission  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_timeline_renders_a_bubble(qt_app):
    from babelfishr.ui.timeline import TimelineView

    timeline = TimelineView()
    tx = Transmission(transcript="hola", translation="hello", source_language="es",
                      duration=2.0, channel_name="GMRS 16", frequency_mhz=462.575,
                      state=ProcessingState.COMPLETE)
    bubble = timeline.add(tx)
    assert timeline.count() == 1
    assert "GMRS 16" in bubble.header.text()
    assert "hola" in bubble.original_label.text()
    assert "hello" in bubble.translated_label.text()


def test_bubble_separates_original_from_translation(qt_app):
    from babelfishr.ui.timeline import TimelineView

    timeline = TimelineView()
    bubble = timeline.add(Transmission(transcript="hola", translation="hello",
                                       source_language="es", target_language="en"))
    assert bubble.original_label.text() != bubble.translated_label.text()
    assert "es" in bubble.original_label.text()
    assert "en" in bubble.translated_label.text()


def test_failed_bubble_offers_retry_and_reassures(qt_app):
    from babelfishr.ui.timeline import TimelineView

    tx = Transmission(transcript="kept")
    tx.fail("translation", "api down")
    bubble = TimelineView().add(tx)
    assert bubble.retry_button.isVisibleTo(bubble)
    assert "api down" in bubble.error_label.text()
    assert "safe" in bubble.error_label.text().lower()


def test_provisional_text_is_marked_as_such(qt_app):
    from babelfishr.ui.timeline import TimelineView

    bubble = TimelineView().add(Transmission())
    bubble.set_provisional("partial words")
    assert "<i>" in bubble.provisional.text()
    assert "partial words" in bubble.provisional.text()


def test_meter_widget_paints(qt_app):
    from babelfishr.ui.widgets import LevelMeterWidget

    meter = LevelMeterWidget()
    meter.resize(200, 22)
    meter.set_reading(0.6, 0.8, True, 5)
    assert not meter.grab().isNull()


def test_main_window_states_and_banners(qt_app, config, store):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(BabelFishRApp(config=config, store=store))
    # The badge carries a symbol as well as the word, so state is never
    # conveyed by colour alone.
    assert "Idle" in window.state_badge.text()
    # Local-only engines must say so plainly.
    assert "computer" in window.privacy_banner.text()
    # A mock engine must be called out, not quietly accepted.
    assert window.warning_banner.isVisibleTo(window)
    assert "MOCK" in window.warning_banner.text()
    window.close()


def test_main_window_warns_when_no_profile_is_selected(qt_app, config, store):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(BabelFishRApp(config=config, store=store))
    assert "cannot determine" in window.channel_label.text()
    window.close()


def test_replay_populates_the_timeline(qt_app, config, store, fixture_wav,
                                       expected_transmissions):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    window = MainWindow(app)
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="ui-replay")
    app.run_replay()
    window._drain_events()
    qt_app.processEvents()
    assert window.timeline.count() == expected_transmissions
    app.stop_session()
    window.close()


# ---- redesign: appearance adaptivity and accessibility ----------------
def test_theme_adapts_to_appearance(qt_app):
    from babelfishr.ui import theme

    css = theme.stylesheet()
    assert css and "{" in css
    # The old build hard-coded a GitHub-dark palette regardless of appearance.
    for banned in ("#161b22", "#0d1117", "#30363d", "#8b949e"):
        assert banned not in css, f"hard-coded palette colour {banned} remains"


def test_theme_tokens_come_from_the_system_palette(qt_app):
    from babelfishr.ui import theme

    tokens = theme.tokens()
    for key in ("window", "surface", "border", "text", "accent"):
        assert tokens[key].startswith("#")
    assert isinstance(theme.is_dark(), bool)


def test_state_is_not_conveyed_by_colour_alone(qt_app, config, store):
    """Each state carries a symbol and a word, not just a colour."""
    from babelfishr.app import BabelFishRApp
    from babelfishr.pipeline import PipelineState
    from babelfishr.ui.main_window import STATE_TEXT, MainWindow

    for state, (text, tone, symbol) in STATE_TEXT.items():
        assert text and symbol, f"{state} lacks a text or symbol cue"

    window = MainWindow(BabelFishRApp(config=config, store=store))
    window._set_state(PipelineState.RECEIVING)
    assert "Receiving" in window.state_badge.text()
    assert window.state_badge.text() != "Receiving"  # symbol present too
    window.close()


def test_mode_badge_shows_the_operating_mode(qt_app, config, store):
    from babelfishr.app import BabelFishRApp
    from babelfishr.modes import OperatingMode
    from babelfishr.ui.main_window import MainWindow

    config.mode = OperatingMode.FIELD_OFFLINE.value
    window = MainWindow(BabelFishRApp(config=config, store=store))
    assert window.mode_badge.text() == "FIELD OFFLINE"
    window.close()


def test_readiness_indicator_reflects_capability(qt_app, config, store):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(BabelFishRApp(config=config, store=store))
    assert window.ready_badge.text()
    assert window.ready_badge.accessibleDescription()
    window.close()


def test_setup_panel_collapses(qt_app, config, store):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(BabelFishRApp(config=config, store=store))
    assert window.setup_box.isCheckable() and window.setup_box.isChecked()
    window.setup_box.setChecked(False)
    assert not window.setup_box.isChecked()
    window.close()


def test_controls_have_accessible_names(qt_app, config, store):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(BabelFishRApp(config=config, store=store))
    for widget in (window.start_button, window.state_badge, window.mode_badge,
                   window.ready_badge):
        assert widget.accessibleName() or widget.text(), (
            f"{widget.objectName()} has no accessible label")
    window.close()


def test_secondary_actions_live_in_an_overflow_menu(qt_app):
    from babelfishr.ui.timeline import TimelineView

    bubble = TimelineView().add(Transmission(transcript="hi", audio_path="/x.wav"))
    labels = {a.text() for a in bubble.menu_button.menu().actions() if a.text()}
    for expected in ("Edit transcript and translation...", "Edit tags...",
                     "Bookmark", "Transcribe anyway", "Export audio..."):
        assert expected in labels, f"{expected} missing from the overflow menu"
    # Automatic hunting and a specific-mode submenu are both offered.
    assert any(label.startswith("Analyze as digital") for label in labels)
    assert any("specific mode" in label for label in labels)


def test_skipped_recording_offers_transcribe_anyway(qt_app):
    from babelfishr.models import ContentClass
    from babelfishr.ui.timeline import TimelineView

    tx = Transmission(audio_path="/tmp/x.wav", state=ProcessingState.SKIPPED,
                      content_class=ContentClass.NOISE,
                      skip_reason="Classified as noise. The recording is kept.")
    bubble = TimelineView().add(tx)
    assert bubble.action_button.isVisibleTo(bubble)
    assert "recording is kept" in bubble.notes_label.text()


def test_digital_result_is_shown_with_decoded_playback(qt_app):
    from babelfishr.models import AnalysisAttempt, AnalysisOutcome
    from babelfishr.ui.timeline import TimelineView

    tx = Transmission(audio_path="/tmp/x.wav")
    attempt = AnalysisAttempt(engine="dsd-neo", engine_version="1.2.3",
                              outcome=AnalysisOutcome.VOICE_DECODED,
                              protocol="DMR")
    attempt.artifacts.append(
        __import__("babelfishr.models", fromlist=["AnalysisArtifact"])
        .AnalysisArtifact(kind="decoded-audio", path="/tmp/decoded.wav"))
    tx.analysis_attempts.append(attempt)
    bubble = TimelineView().add(tx)
    assert bubble.decoded_button.isVisibleTo(bubble)
    assert "DMR" in bubble.notes_label.text()


def test_operator_entered_frequency_is_labelled_in_the_ui(qt_app):
    from babelfishr.models import Provenance
    from babelfishr.ui.timeline import TimelineView

    tx = Transmission(frequency_mhz=462.575,
                      frequency_provenance=Provenance.PROFILE)
    bubble = TimelineView().add(tx)
    assert "(entered)" in bubble.header.text()

    measured = Transmission(frequency_mhz=462.575,
                            frequency_provenance=Provenance.SDR)
    bubble2 = TimelineView().add(measured)
    assert "(entered)" not in bubble2.header.text()


def test_empty_state_reassures_about_recording(qt_app):
    from babelfishr.ui.timeline import TimelineView

    timeline = TimelineView()  # keep a reference; Qt deletes orphaned widgets
    assert "recorded before it is processed" in timeline.empty_label.text()


def test_readiness_dialog_builds(qt_app, config, store):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.readiness_dialog import ReadinessDialog

    dialog = ReadinessDialog(BabelFishRApp(config=config, store=store))
    assert dialog.tree.topLevelItemCount() > 0
    assert dialog.headline.text()
    assert not dialog.grab().isNull()


def test_setup_assistant_builds_and_parses_pairs(qt_app, config, store):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.setup_assistant import SetupAssistant

    assistant = SetupAssistant(BabelFishRApp(config=config, store=store))
    assert ("es", "en") in assistant.language_pairs()
    assert "prepare-field" in assistant.command_label.text()
    assert not assistant.grab().isNull()


@pytest.mark.parametrize("dark", [False, True])
def test_window_renders_in_both_appearances(qt_app, config, store, dark,
                                            monkeypatch):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui import theme
    from babelfishr.ui.main_window import MainWindow

    monkeypatch.setattr(theme, "is_dark", lambda widget=None: dark)
    window = MainWindow(BabelFishRApp(config=config, store=store))
    window.resize(1000, 700)
    assert not window.grab().isNull()
    window.close()
