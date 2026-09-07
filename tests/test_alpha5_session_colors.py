"""Session tab colours: chosen per tab, bound to the Session's id, persistent.

Driven through the tab's right-click menu and the colour dialog (the dialog
itself is substituted - it is Qt's, and blocks for a person), then checked in
the tab bar, in the store, and in a freshly opened store.
"""

from __future__ import annotations

import os
import sqlite3
import time

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.models import ProcessingState
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
from babelfishr.storage import Store
from babelfishr.testing import build_fixture

SR = 48_000


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def wav(tmp_path) -> str:
    return build_fixture([{"gap": 1.0},
                          {"kind": "voice", "duration": 2.0, "level_dbfs": -14},
                          {"gap": 1.0}], sample_rate=SR).write(
        str(tmp_path / "voice.wav"))


def mock_app(config, store) -> BabelFishRApp:
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    return app


def pump(qt_app, rounds: int = 20) -> None:
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.005)


def tab_index_for(window, conversation_id) -> int:
    for index in range(window.session_tabs.count()):
        if window.session_tabs.tabData(index) == conversation_id:
            return index
    raise AssertionError(f"no tab for {conversation_id}")


def answer_color(monkeypatch, hex_or_none):
    """Stand in for QColorDialog.getColor: a colour, or Cancel (invalid)."""
    from PySide6 import QtGui, QtWidgets

    calls = []

    def fake(initial=None, parent=None, title="", *args, **kwargs):
        calls.append((initial, title))
        return QtGui.QColor(hex_or_none) if hex_or_none else QtGui.QColor()

    monkeypatch.setattr(QtWidgets.QColorDialog, "getColor", staticmethod(fake))
    return calls


def menu_action(window, index, prefix):
    menu = window._build_session_tab_menu(index)
    found = [a for a in menu.actions() if a.text().startswith(prefix)]
    assert found, [a.text() for a in menu.actions()]
    return found[0]


# ---- choosing ---------------------------------------------------------------


def test_the_tab_menu_offers_a_colour_and_the_choice_is_shown(qt_app, config,
                                                              store,
                                                              monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    alpha = app.create_conversation("Alpha")
    bravo = app.create_conversation("Bravo")
    window = MainWindow(app)
    window.show()
    pump(qt_app)

    index = tab_index_for(window, alpha.id)
    assert window.session_tabs.tabIcon(index).isNull(), "a colour before any choice"
    calls = answer_color(monkeypatch, "#ff8800")
    menu_action(window, index, "Tab colour").trigger()
    pump(qt_app)

    assert calls and "Alpha" in calls[0][1], "the dialog did not name the Session"
    index = tab_index_for(window, alpha.id)
    assert not window.session_tabs.tabIcon(index).isNull(), "no swatch shown"
    assert store.get_conversation(alpha.id).color == "#ff8800"
    # Only that tab.
    other = tab_index_for(window, bravo.id)
    assert window.session_tabs.tabIcon(other).isNull()
    assert store.get_conversation(bravo.id).color == ""
    # The name is untouched and still readable in the theme's text colour.
    assert window.session_tabs.tabText(index) == "Alpha"
    assert "set to #ff8800" in window.status.currentMessage()
    window.close()


def test_cancel_changes_nothing(qt_app, config, store, monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    alpha = app.create_conversation("Alpha")
    store.set_conversation_color(alpha.id, "#112233")
    window = MainWindow(app)
    pump(qt_app)
    index = tab_index_for(window, alpha.id)
    answer_color(monkeypatch, None)             # Cancel
    menu_action(window, index, "Tab colour").trigger()
    assert store.get_conversation(alpha.id).color == "#112233"
    assert "unchanged" in window.status.currentMessage()
    window.close()


def test_default_colour_resets_only_that_tab(qt_app, config, store):
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    alpha = app.create_conversation("Alpha")
    bravo = app.create_conversation("Bravo")
    store.set_conversation_color(alpha.id, "#aa0000")
    store.set_conversation_color(bravo.id, "#00aa00")
    window = MainWindow(app)
    pump(qt_app)

    index = tab_index_for(window, alpha.id)
    reset = menu_action(window, index, "Default tab colour")
    assert reset.isEnabled()
    reset.trigger()
    pump(qt_app)
    assert store.get_conversation(alpha.id).color == ""
    assert window.session_tabs.tabIcon(tab_index_for(window, alpha.id)).isNull()
    assert store.get_conversation(bravo.id).color == "#00aa00"
    assert not window.session_tabs.tabIcon(tab_index_for(window, bravo.id)).isNull()
    # With no colour left, the reset item is disabled rather than a no-op.
    assert not menu_action(window, tab_index_for(window, alpha.id),
                           "Default tab colour").isEnabled()
    window.close()


# ---- binding and persistence --------------------------------------------


def test_the_colour_follows_the_session_through_rename_and_reorder(
        qt_app, config, store, monkeypatch):
    from PySide6 import QtWidgets

    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    alpha = app.create_conversation("Alpha")
    charlie = app.create_conversation("Charlie")
    store.set_conversation_color(alpha.id, "#3366cc")
    window = MainWindow(app)
    pump(qt_app)

    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("Zulu", True)))
    window._rename_session_tab(tab_index_for(window, alpha.id))
    pump(qt_app)
    renamed = store.get_conversation(alpha.id)
    assert renamed.name == "Zulu" and renamed.color == "#3366cc"
    assert not window.session_tabs.tabIcon(tab_index_for(window, alpha.id)).isNull()

    # Move it to the end of the row: colour stays with the id.
    charlie_row = store.get_conversation(charlie.id)
    charlie_row.position = 0
    store.save_conversation(charlie_row)
    renamed.position = 99
    store.save_conversation(renamed)
    window._refresh_session_tabs()
    assert window.session_tabs.tabData(window.session_tabs.count() - 1) == alpha.id
    assert not window.session_tabs.tabIcon(window.session_tabs.count() - 1).isNull()
    assert window.session_tabs.tabIcon(tab_index_for(window, charlie.id)).isNull()
    window.close()


def test_the_colour_survives_switching_tabs_and_restarting(qt_app, config,
                                                            store):
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    alpha = app.create_conversation("Alpha")
    store.set_conversation_color(alpha.id, "#123456")
    window = MainWindow(app)
    pump(qt_app)
    general = store.default_conversation()
    window.session_tabs.setCurrentIndex(tab_index_for(window, general.id))
    pump(qt_app)
    window.session_tabs.setCurrentIndex(tab_index_for(window, alpha.id))
    pump(qt_app)
    assert not window.session_tabs.tabIcon(tab_index_for(window, alpha.id)).isNull()
    window.close()

    reopened = Store(config.database, recordings_dir=config.recording.directory)
    assert reopened.get_conversation(alpha.id).color == "#123456"
    app2 = mock_app(config, reopened)
    window2 = MainWindow(app2)
    pump(qt_app)
    assert not window2.session_tabs.tabIcon(tab_index_for(window2, alpha.id)).isNull()
    window2.close()


def test_the_selected_tab_is_still_the_selected_tab_in_every_colour(qt_app,
                                                                    config,
                                                                    store):
    """The colour is a swatch; selection is Qt's own marking, unchanged."""
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    alpha = app.create_conversation("Alpha")
    window = MainWindow(app)
    pump(qt_app)
    index = tab_index_for(window, alpha.id)
    for color in ("#000000", "#ffffff", "#ff0000", "#00ff00", "#0000ff",
                  "#888888"):
        store.set_conversation_color(alpha.id, color)
        window._refresh_session_tabs()
        window.session_tabs.setCurrentIndex(index)
        pump(qt_app, 5)
        assert window.session_tabs.currentIndex() == index
        assert window.session_tabs.tabText(index) == "Alpha"
        # The label colour is the theme's, not the swatch's.
        assert window.session_tabs.tabTextColor(index).name() != color \
            or color in ("#000000", "#ffffff")
    window.close()


def test_only_a_real_hex_colour_is_stored(config, store):
    conversation = store.create_conversation("Alpha")
    with pytest.raises(ValueError):
        store.set_conversation_color(conversation.id, "red")
    with pytest.raises(ValueError):
        store.set_conversation_color(conversation.id, "#12345")
    assert store.get_conversation(conversation.id).color == ""
    store.set_conversation_color(conversation.id, "#ABCDEF")
    assert store.get_conversation(conversation.id).color == "#abcdef"
    assert store.set_conversation_color("conv_missing", "#000000") is None


# ---- migration ---------------------------------------------------------------


def _schema_4_database(path: str, fixture_ddl: str) -> None:
    """A database as run 21 left it: schema 3 plus the schema-4 columns,
    two conversations, two runs, two messages."""
    import pathlib

    fixture = pathlib.Path(__file__).resolve().parent / "fixtures" / "schema_3.sql"
    lines = fixture.read_text().split("\n")
    first = next(i for i, line in enumerate(lines) if not line.startswith("--"))
    conn = sqlite3.connect(path)
    conn.executescript("\n".join(lines[first:]))
    # The columns the schema-4 migration added, exactly as it added them.
    for table, column, ddl in (
            ("sessions", "conversation_id", "TEXT"),
            ("transmissions", "snr_db", "REAL"),
            ("transmissions", "snr_provenance", "TEXT DEFAULT 'unknown'"),
            ("transmissions", "squelch_code", "TEXT DEFAULT ''"),
            ("transmissions", "squelch_code_provenance", "TEXT DEFAULT 'unknown'"),
            ("transmissions", "talkgroup", "TEXT DEFAULT ''"),
            ("transmissions", "talkgroup_provenance", "TEXT DEFAULT 'unknown'"),
            ("transmissions", "unit_id", "TEXT DEFAULT ''"),
            ("transmissions", "unit_id_provenance", "TEXT DEFAULT 'unknown'"),
            ("transmissions", "protocol", "TEXT DEFAULT ''"),
            ("transmissions", "protocol_provenance", "TEXT DEFAULT 'unknown'"),
            ("transmissions", "signal_metadata", "TEXT DEFAULT '{}'")):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    # Schema 4 created this table with exactly these columns - no colour.
    conn.execute("""CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
        is_default INTEGER DEFAULT 0, position INTEGER DEFAULT 0,
        notes TEXT DEFAULT '')""")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', '4')")
    conn.execute("INSERT INTO conversations (id, name, created_at, is_default, "
                 "position, notes) VALUES ('conv_gen', 'General', "
                 "'2026-01-01T00:00:00+00:00', 1, 0, '')")
    conn.execute("INSERT INTO conversations (id, name, created_at, is_default, "
                 "position, notes) VALUES ('conv_ops', 'Ops', "
                 "'2026-01-01T00:00:00+00:00', 0, 1, 'kept note')")
    for run, conv in (("sess_a", "conv_gen"), ("sess_b", "conv_ops")):
        conn.execute("INSERT INTO sessions (id, name, started_at, target_language, "
                     "conversation_id) VALUES (?,?,?,?,?)",
                     (run, run, "2026-01-01T00:00:00+00:00", "en", conv))
        conn.execute(
            """INSERT INTO transmissions (id, session_id, started_at, duration,
                   audio_path, transcript, translation, state, tags, bookmarked)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (f"tx_{run}", run, "2026-01-01T00:01:00+00:00", 2.0,
             f"/rec/{run}.wav", f"words {run}", f"translated {run}",
             "complete", '["a"]', 1))
    conn.commit()
    conn.close()


def test_a_run_21_database_gains_colours_without_losing_anything(tmp_path):
    database = str(tmp_path / "run21.sqlite3")
    _schema_4_database(database, "")

    store = Store(database, recordings_dir=str(tmp_path))
    assert store.schema_version == 5
    conversations = {c.id: c for c in store.list_conversations()}
    assert set(conversations) == {"conv_gen", "conv_ops"}
    assert conversations["conv_gen"].is_default
    assert conversations["conv_ops"].name == "Ops"
    assert conversations["conv_ops"].notes == "kept note"
    assert all(c.color == "" for c in conversations.values())
    for run, conv in (("sess_a", "conv_gen"), ("sess_b", "conv_ops")):
        assert store.session_ids_for_conversation(conv) == [run]
        (tx,) = store.conversation_transmissions(conv, limit=10)
        assert tx.id == f"tx_{run}"
        assert tx.transcript == f"words {run}"
        assert tx.translation == f"translated {run}"
        assert tx.audio_path == f"/rec/{run}.wav"
        assert tx.tags == ["a"] and tx.bookmarked is True
        assert tx.state is ProcessingState.COMPLETE

    store.set_conversation_color("conv_ops", "#00ffaa")
    store.close()
    again = Store(database, recordings_dir=str(tmp_path))
    assert again.get_conversation("conv_ops").color == "#00ffaa"
    assert again.get_conversation("conv_gen").color == ""
    assert again.schema_version == 5
    again.close()


def test_the_migration_is_idempotent_at_schema_5(tmp_path):
    database = str(tmp_path / "run21.sqlite3")
    _schema_4_database(database, "")
    for _ in range(3):
        store = Store(database, recordings_dir=str(tmp_path))
        assert len(store.list_conversations()) == 2
        assert store.schema_version == 5
        store.close()
