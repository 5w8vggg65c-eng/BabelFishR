"""The application's menus are reachable inside its own window.

On the first live bench test the operator reported that the File / View /
Tools / Help menus he was told to use were "desktop menus" and that BabelFishR
offered none of those commands through the instructed route. The cause on his
Mac is not established here. What is established: the menu bar is now asked to
stay inside the window, every View and Tools command is reached in these tests
by *clicking* the visible bar and then the item - never by calling the handler
- and the commands do what the checklist says they do.

What these tests cannot do: they run on Linux, offscreen. Qt reports
isNativeMenuBar() False here whether or not the request was made, and the bar
is drawn in-window here regardless. So the wiring is proven, and the request
is proven to be made (by reading the source), but whether macOS honours it is
not something this file can show.
"""

from __future__ import annotations

import ast
import inspect
import os
import time

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.models import ProcessingState
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
from babelfishr.testing import build_fixture

SR = 48_000
PHRASE = "purple giraffe seventeen"


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
                          {"gap": 1.0},
                          {"kind": "voice", "duration": 2.0, "level_dbfs": -12},
                          {"gap": 1.0}], sample_rate=SR).write(
        str(tmp_path / "voice.wav"))


def pump(qt_app, rounds: int = 30, pause: float = 0.005) -> None:
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(pause)


def mock_app(config, store) -> BabelFishRApp:
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    return app


def shown_window(qt_app, app):
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(app)
    window.resize(1000, 800)
    window.show()
    pump(qt_app)
    return window


def menu_titles(window):
    return [a.text().replace("&", "") for a in window.menuBar().actions()]


def top_level_action(window, title):
    for action in window.menuBar().actions():
        if action.text().replace("&", "") == title:
            return action
    raise AssertionError(f"no {title!r} menu; have {menu_titles(window)}")


def click_menu_item(qt_app, window, title: str, item):
    """Open a menu by clicking its title on the bar, then click the item.

    Two real mouse clicks on the visible widgets. No .trigger(), no direct
    call. Asserts the popup actually opened in between, so a click that
    landed on nothing cannot pass by way of a shortcut or a stray call.
    """
    from PySide6 import QtCore
    from PySide6.QtTest import QTest

    bar = window.menuBar()
    assert bar.isVisible(), "the menu bar is not shown"
    action = top_level_action(window, title)
    menu = action.menu()
    QTest.mouseClick(bar, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier,
                     bar.actionGeometry(action).center())
    pump(qt_app)
    assert menu.isVisible(), f"clicking {title!r} on the bar opened no menu"
    if isinstance(item, str):
        matches = [a for a in menu.actions() if a.text() == item]
        assert matches, f"{item!r} is not in {title}: {[a.text() for a in menu.actions()]}"
        item = matches[0]
    QTest.mouseClick(menu, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier,
                     menu.actionGeometry(item).center())
    pump(qt_app)
    assert not menu.isVisible(), "the menu stayed open after the click"


def answer_dialog(monkeypatch, text, accepted=True):
    from PySide6 import QtWidgets

    monkeypatch.setattr(QtWidgets.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: (text, accepted)))


def two_sessions(app, store, wav):
    """Alpha holds the unique phrase; Bravo holds ordinary traffic.

    One Alpha transmission is made reviewable (low confidence) so the review
    queue has a known, non-empty answer; everything else is confident.
    """
    alpha = app.create_conversation("Alpha")
    bravo = app.create_conversation("Bravo")

    app.select_conversation(alpha.id)
    app.start_session(replay_path=wav, name="alpha run")
    app.run_replay()
    app.stop_session()
    app.select_conversation(bravo.id)
    app.start_session(replay_path=wav, name="bravo run")
    app.run_replay()
    app.stop_session()

    alpha_txs = app.recent_transmissions(conversation_id=alpha.id)
    bravo_txs = app.recent_transmissions(conversation_id=bravo.id)
    assert len(alpha_txs) >= 2 and bravo_txs, "not enough traffic to test with"
    for tx in alpha_txs + bravo_txs:
        tx.transcript_confidence = 0.95
        tx.language_confidence = 0.95
        tx.state = ProcessingState.COMPLETE
        tx.transcript = "routine traffic"
    alpha_txs[0].transcript = f"{PHRASE} on the ridge"
    alpha_txs[1].transcript_confidence = 0.2          # reviewable
    for tx in alpha_txs + bravo_txs:
        store.save_transmission(tx)
    return alpha, bravo, alpha_txs, bravo_txs


# ---- 1. the bar is inside the window --------------------------------------


def test_the_menu_bar_is_drawn_inside_the_window(qt_app, config, store):
    """On this platform: the bar has a size, sits at the window's top edge,
    and the central content starts below it. Not a Mac result."""
    window = shown_window(qt_app, mock_app(config, store))
    bar = window.menuBar()
    assert bar.isVisible() and bar.isEnabled()
    assert bar.height() > 0 and bar.width() > 0
    assert window.rect().contains(bar.geometry()), "the bar is outside the window"
    assert window.centralWidget().geometry().top() >= bar.geometry().bottom(), (
        "the content is drawn over the menu bar")
    assert menu_titles(window) == ["File", "View", "Receiver", "Tools", "Help"]
    for title in menu_titles(window):
        assert top_level_action(window, title).menu().actions(), f"{title} is empty"
    window.close()


def test_the_in_window_menu_bar_is_requested_in_the_source():
    """Wiring, not platform: _build_menu asks for a non-native bar.

    isNativeMenuBar() is False on Linux no matter what, so the property is
    not evidence here. The request is. Whether macOS honours it is untested.
    """
    from babelfishr.ui.main_window import MainWindow

    tree = ast.parse(inspect.getsource(MainWindow._build_menu).lstrip())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "setNativeMenuBar"]
    assert calls, "_build_menu never calls setNativeMenuBar"
    assert all(len(c.args) == 1 and isinstance(c.args[0], ast.Constant)
               and c.args[0].value is False for c in calls), (
        "setNativeMenuBar is called with something other than False")


def test_collapsing_session_options_leaves_the_menus_alone(qt_app, config,
                                                            store):
    window = shown_window(qt_app, mock_app(config, store))
    bar = window.menuBar()
    before = bar.geometry()
    for _ in range(3):
        window.setup_box.setChecked(False)
        pump(qt_app)
        assert bar.isVisible() and bar.isEnabled()
        assert bar.geometry() == before
        assert all(a.isEnabled() for a in bar.actions())
        window.setup_box.setChecked(True)
        pump(qt_app)
        assert bar.isVisible() and bar.geometry() == before
    window.close()


# ---- 2. View, by clicking -------------------------------------------------


def test_search_from_the_view_menu_finds_the_phrase_in_alpha_only(
        qt_app, config, store, wav, monkeypatch):
    app = mock_app(config, store)
    alpha, bravo, alpha_txs, bravo_txs = two_sessions(app, store, wav)
    app.select_conversation(alpha.id)
    window = shown_window(qt_app, app)
    window._refresh_session_tabs()
    window._reload_timeline()
    assert set(window.timeline._bubbles) == {t.id for t in alpha_txs}

    answer_dialog(monkeypatch, "giraffe")
    click_menu_item(qt_app, window, "View", "Search transmissions...")
    assert set(window.timeline._bubbles) == {alpha_txs[0].id}, (
        "search from the menu did not narrow the thread to the one match")
    assert "1 match" in window.status.currentMessage()
    assert "Show all transmissions" in window.status.currentMessage()

    click_menu_item(qt_app, window, "View", "Show all transmissions")
    assert set(window.timeline._bubbles) == {t.id for t in alpha_txs}, (
        "Show all did not restore Alpha's thread")

    # Bravo: the same phrase is not there, and the search says so.
    window.session_tabs.setCurrentIndex(
        next(i for i in range(window.session_tabs.count())
             if window.session_tabs.tabData(i) == bravo.id))
    pump(qt_app)
    assert app.conversation_id == bravo.id
    assert set(window.timeline._bubbles) == {t.id for t in bravo_txs}
    click_menu_item(qt_app, window, "View", "Search transmissions...")
    assert window.timeline.count() == 0, "Alpha's phrase was found in Bravo"
    assert "0 match" in window.status.currentMessage()
    click_menu_item(qt_app, window, "View", "Show all transmissions")
    assert set(window.timeline._bubbles) == {t.id for t in bravo_txs}
    window.close()


def test_review_queue_from_the_view_menu_shows_the_known_reviewable(
        qt_app, config, store, wav):
    app = mock_app(config, store)
    alpha, bravo, alpha_txs, bravo_txs = two_sessions(app, store, wav)
    app.select_conversation(alpha.id)
    window = shown_window(qt_app, app)
    window._refresh_session_tabs()
    window._reload_timeline()

    click_menu_item(qt_app, window, "View", "Review queue")
    assert set(window.timeline._bubbles) == {alpha_txs[1].id}, (
        "the review queue did not show exactly the low-confidence transmission")
    assert window.timeline.count() == 1, "an empty queue must not pass here"
    assert "1 transmission" in window.status.currentMessage()

    click_menu_item(qt_app, window, "View", "Show all transmissions")
    assert set(window.timeline._bubbles) == {t.id for t in alpha_txs}

    # Bravo has nothing to review; that is the honest answer there.
    app.select_conversation(bravo.id)
    window._refresh_session_tabs()
    window._reload_timeline()
    click_menu_item(qt_app, window, "View", "Review queue")
    assert window.timeline.count() == 0
    click_menu_item(qt_app, window, "View", "Show all transmissions")
    assert set(window.timeline._bubbles) == {t.id for t in bravo_txs}
    window.close()


def test_a_cancelled_search_changes_nothing(qt_app, config, store, wav,
                                            monkeypatch):
    app = mock_app(config, store)
    alpha, _, alpha_txs, _ = two_sessions(app, store, wav)
    app.select_conversation(alpha.id)
    window = shown_window(qt_app, app)
    window._refresh_session_tabs()
    window._reload_timeline()
    answer_dialog(monkeypatch, "giraffe", accepted=False)
    click_menu_item(qt_app, window, "View", "Search transmissions...")
    assert set(window.timeline._bubbles) == {t.id for t in alpha_txs}
    window.close()


# ---- 3. Tools and Help, by clicking ---------------------------------------


def test_the_tools_menu_reaches_the_existing_setup_and_diagnostics(
        qt_app, config, store, monkeypatch, tmp_path):
    from PySide6 import QtWidgets

    from babelfishr.ui import readiness_dialog, setup_assistant

    app = mock_app(config, store)
    window = shown_window(qt_app, app)
    tools = top_level_action(window, "Tools").menu()
    labels = [a.text() for a in tools.actions() if a.text()]
    for expected in ("Field readiness...", "Setup assistant...",
                     "Copy Diagnostic Report", "Reveal Logs in Finder"):
        assert expected in labels, labels

    opened = []
    monkeypatch.setattr(readiness_dialog.ReadinessDialog, "exec",
                        lambda self: opened.append("readiness") or 0)
    monkeypatch.setattr(setup_assistant.SetupAssistant, "exec",
                        lambda self: opened.append("assistant") or 0)
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: opened.append("info")))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: opened.append("warning")))

    click_menu_item(qt_app, window, "Tools", "Field readiness...")
    assert "readiness" in opened, "Field readiness did not open its dialog"
    click_menu_item(qt_app, window, "Tools", "Setup assistant...")
    assert "assistant" in opened, "Setup assistant did not open"

    report = window.diagnostic_report_path()
    if report.exists():
        report.unlink()
    click_menu_item(qt_app, window, "Tools", "Copy Diagnostic Report")
    assert report.exists() and report.stat().st_size > 0, (
        "Copy Diagnostic Report wrote no report")
    assert "info" in opened
    assert "warning" not in opened
    window.close()


def test_the_help_menu_is_wired(qt_app, config, store, monkeypatch):
    from PySide6 import QtWidgets

    app = mock_app(config, store)
    window = shown_window(qt_app, app)
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown.append(a[1])))
    click_menu_item(qt_app, window, "Help", "Where are my recordings?")
    assert shown and shown[-1] == "Storage"
    window.close()
