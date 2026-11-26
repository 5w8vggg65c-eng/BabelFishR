"""Operator approval and inline corrections on actual timeline widgets."""
from __future__ import annotations

import dataclasses

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.models import ProcessingState, Session, Transmission


def flagged(store, **values):
    session = Session()
    store.save_session(session)
    fields = dict(session_id=session.id, state=ProcessingState.COMPLETE,
                  source_language="es", target_language="en", transcript="quince",
                  translation="fifteen", transcript_confidence=0.4,
                  signal_metadata={"speech_check": {"verdict": "uncertain"}})
    fields.update(values)
    return store.save_transmission(Transmission(**fields))


def test_approval_clears_check_without_editing_or_masking_missing_audio(config, store):
    app = BabelFishRApp(config=config, store=store)
    tx = flagged(store)
    late = store.get_transmission(tx.id)
    assert [row.id for row in store.review_queue()] == [tx.id]
    changed = app.approve(tx.id)
    assert changed.operator_checked and not changed.needs_review
    assert changed.corrected_at is None and not changed.has_correction
    assert not store.review_queue()
    assert store.is_stale(late)
    # A background snapshot can still publish new results without undoing
    # the operator's action or replaying any corrections over engine text.
    late.translation = "15"
    store.save_transmission(late)
    reread = store.get_transmission(tx.id)
    assert reread.operator_checked and reread.translation == "15"
    assert reread.transcript == "quince" and not reread.audio_path
    assert not store.review_queue()
    # A processing error stays actionable, even on a reviewed message.
    reread.fail("translation", "unavailable")
    store.save_transmission(reread)
    assert app.approve(tx.id) is None
    assert [row.id for row in store.review_queue()] == [tx.id]
    app.close()


def test_edit_survives_late_save_and_new_recognition_invalidates_approval(config, store):
    app = BabelFishRApp(config=config, store=store)
    tx = flagged(store)
    worker = store.get_transmission(tx.id)
    app.approve(tx.id)
    app.correct(tx.id, transcript="cincuenta", translation="fifty")
    worker.translation = "15"
    store.save_transmission(worker)
    actual = store.get_transmission(tx.id)
    assert actual.translation == "15" and actual.transcript == "quince"
    assert actual.transcript_correction == "cincuenta" and actual.translation_correction == "fifty"
    assert actual.operator_checked
    actual.state = ProcessingState.CAPTURED
    store.save_transmission(actual, new_recognition=True)
    actual = store.get_transmission(tx.id)
    assert not actual.operator_checked and actual.needs_review
    assert "operator_approved" not in actual.signal_metadata
    assert actual.signal_metadata["superseded_corrections"][-1]["translation"] == "fifty"
    assert actual.transcript == "quince" and actual.translation == "15"
    app.close()


def test_removing_invented_words_keeps_raw_outputs_and_visible_pair_status(config, store):
    pytest.importorskip("PySide6")
    from babelfishr.ui.timeline import pairing_status

    app = BabelFishRApp(config=config, store=store)
    tx = flagged(store)
    app.correct(tx.id, transcript="", translation="")
    saved = store.get_transmission(tx.id)
    assert saved.operator_checked and not saved.needs_review
    assert saved.transcript == "quince" and saved.translation == "fifteen"
    assert saved.display_transcript == saved.shown_transcript == ""
    assert saved.display_translation == saved.shown_translation == ""
    assert "transcript cleared" in pairing_status(saved)[0].lower()
    assert not store.review_queue()
    app.close()


def test_inline_widgets_keep_the_draft_and_cancel_or_save_on_the_same_row(config, store):
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets
    from babelfishr.ui.timeline import TimelineView

    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app = BabelFishRApp(config=config, store=store)
    tx = flagged(store)
    view = TimelineView()
    view.correctionRequested.connect(lambda row, words, translation:
                                     app.correct(row, transcript=words, translation=translation))
    view.approvalRequested.connect(app.approve)
    transitions = []
    view.editingChanged.connect(transitions.append)
    bubble = view.add(tx)
    assert not bubble.approve_button.isHidden()
    assert not bubble.edit_button.isHidden() and not hasattr(bubble, "playback_bar")
    bubble.edit_button.click()
    assert view.editing_id == tx.id == view.selected_id
    assert not bubble.original_label.isReadOnly() and not bubble.translated_label.isReadOnly()
    bubble.original_label.setPlainText("draft")
    view.update(dataclasses.replace(tx, notes="received after edit started"))
    view.set_transmissions([tx], keep_playback=True)
    assert view.bubble(tx.id) is bubble and bubble.original_label.toPlainText() == "draft"
    bubble.cancel_edit_button.click()
    assert bubble.original_label.isReadOnly() and bubble.original_label.text() == "quince"
    assert view.editing_id is None and not store.get_transmission(tx.id).reviewed
    bubble.edit_button.click()
    bubble.original_label.setPlainText("cincuenta")
    bubble.translated_label.setPlainText("fifty")
    bubble.save_edit_button.click()
    updated = store.get_transmission(tx.id)
    view.update(updated)
    assert view.editing_id is None
    assert updated.transcript == "quince" and updated.translation == "fifteen"
    assert bubble.original_label.text() == "cincuenta" and bubble.translated_label.text() == "fifty"
    assert transitions == [tx.id, None, tx.id, None]
    other = flagged(store)
    card = view.add(other)
    card.approve_button.click()
    view.update(store.get_transmission(other.id))
    assert card.review_label.text() == "APPROVED" and card.approve_button.isHidden()
    assert "Audio missing" in card.pairing_label.text()
    view.close()
    application.processEvents()
    app.close()
