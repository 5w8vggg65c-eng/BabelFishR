"""The Transmission entity and its vocabulary guarantees."""

from __future__ import annotations

import datetime as dt

import pytest

from babelfishr.models import (ErrorInfo, ProcessingState, RadioProfile, Session,
                               SourceLanguageMode, TranscriptSegment,
                               Transmission, utcnow)


def test_transmission_round_trips_through_dict():
    tx = Transmission(transcript="hola", translation="hello", source_language="es",
                      tags=["a"], transcript_segments=[TranscriptSegment(0, 1, "hola")])
    tx.finish()
    restored = Transmission.from_dict(tx.to_dict())
    assert restored.to_dict() == tx.to_dict()


def test_correction_never_replaces_the_original():
    tx = Transmission(transcript="original", translation="translated")
    tx.transcript_correction = "corrected"
    assert tx.transcript == "original"
    assert tx.display_transcript == "corrected"
    assert tx.has_correction


def test_failure_records_a_recoverable_error():
    tx = Transmission(transcript="kept")
    tx.fail("translation", "engine down")
    assert tx.state is ProcessingState.FAILED
    assert tx.error.recoverable and tx.error.stage == "translation"
    assert tx.transcript == "kept", "a failure must not discard earlier work"


def test_repeated_failures_count_retries():
    tx = Transmission()
    tx.fail("transcription", "one")
    tx.fail("transcription", "two")
    assert tx.error.retry_count == 1


def test_needs_review_for_low_confidence():
    assert Transmission(transcript_confidence=0.4).needs_review
    assert not Transmission(transcript_confidence=0.95).needs_review
    assert Transmission(transcript_confidence=0.4, reviewed=True).needs_review is False


def test_needs_review_for_failures():
    tx = Transmission()
    tx.fail("transcription", "x")
    assert tx.needs_review


def test_processing_states_are_classified():
    assert ProcessingState.CAPTURED.is_pending
    assert ProcessingState.COMPLETE.is_terminal
    assert ProcessingState.FAILED.is_terminal
    assert ProcessingState.SKIPPED.is_terminal


def test_finish_computes_duration():
    tx = Transmission()
    tx.started_at = utcnow() - dt.timedelta(seconds=3)
    tx.finish()
    assert tx.duration == pytest.approx(3.0, abs=0.2)


def test_profile_label_reflects_operator_input():
    profile = RadioProfile(name="UV-5R", channel_name="GMRS 16",
                           frequency_mhz=462.5750)
    assert profile.label() == "GMRS 16 - 462.5750 MHz"
    assert RadioProfile(name="Nameless").label() == "Nameless"


def test_session_duration_and_state():
    session = Session()
    assert session.is_open
    session.ended_at = session.started_at + dt.timedelta(seconds=90)
    assert not session.is_open
    assert session.duration == pytest.approx(90.0)


def test_no_outgoing_transmission_vocabulary():
    """A received event must never be named as an outgoing one.

    Checked against the actual API surface - field, method and class names -
    rather than prose, since the module docstring legitimately explains why
    outgoing transmissions are absent.
    """
    import dataclasses

    names = {f.name.lower() for f in dataclasses.fields(Transmission)}
    names |= {n.lower() for n in dir(Transmission) if not n.startswith("_")}
    for banned in ("tx_", "outgoing", "transmit", "send", "ptt", "keyup"):
        offenders = [n for n in names if banned in n]
        assert not offenders, f"outgoing-sounding names: {offenders}"


def test_metadata_defaults_are_empty_not_invented():
    """Nothing may claim a frequency or channel that no one supplied."""
    tx = Transmission()
    assert tx.frequency_mhz is None
    assert tx.channel_name == ""
    assert tx.profile_id is None
