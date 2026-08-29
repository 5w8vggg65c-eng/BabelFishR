"""Entry point for ``Uninstall BabelFishR.app``.

A separate bundle, and a separate program. It contains no capture code, asks
for no microphone entitlement, and is the only thing in the project that can
delete an operator's recordings.

    Uninstall BabelFishR.app/Contents/MacOS/UninstallBabelFishR
    Uninstall BabelFishR.app/Contents/MacOS/UninstallBabelFishR --selftest-dry-run

``--selftest-dry-run`` is the packaged build's proof that the uninstaller
works: it computes the real removal plan and prints it, and deletes nothing.
The release pipeline runs it against a scratch home so that even the self-test
cannot touch anything on the build machine.
"""

from __future__ import annotations

import os
import pathlib
import sys


def _selftest_dry_run(home: str = None) -> int:
    """Prove the plan is computable and that a dry run removes nothing."""
    from babelfishr.uninstall import build_plan, describe_plan, uninstall

    # A supplied home is a scratch root: build_plan then excludes the
    # machine's own /Applications entirely, so the self-test cannot so much as
    # name it, let alone stat it. No argument means the real runtime plan.
    supplied = home or os.environ.get("BABELFISHR_UNINSTALL_HOME")
    plan = build_plan(supplied) if supplied else build_plan()
    print(f"Uninstaller dry run for home: {plan.home}")
    print(f"Applications roots in scope: "
          f"{', '.join(str(root) for root in plan.roots())}")
    if supplied and not plan.contains_only_paths_within_its_roots():
        print("FAIL: the plan reaches outside the supplied root")
        return 1
    print(describe_plan(plan))

    before = [p for p in plan.paths() if os.path.lexists(p)]
    report = uninstall(plan, dry_run=True)
    after = [p for p in plan.paths() if os.path.lexists(p)]
    if before != after:
        print("FAIL: a dry run changed the filesystem")
        return 1
    if not report.dry_run:
        print("FAIL: the report does not say it was a dry run")
        return 1
    print()
    print(report.summary())
    print()
    print(f"ok:   {len(plan.items)} allowlisted items planned, "
          f"{len(before)} present, 0 deleted")
    return 0


def _selftest_import() -> int:
    """The uninstaller must be standalone too."""
    import babelfishr.uninstall  # noqa: F401
    from PySide6 import QtWidgets  # noqa: F401

    import babelfishr.ui.uninstall_window  # noqa: F401

    print("uninstaller imports cleanly")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] == "--selftest-dry-run":
        return _selftest_dry_run(argv[1] if len(argv) > 1 else None)
    if argv == ["--selftest-import"]:
        return _selftest_import()
    if argv:
        print(__doc__)
        return 2

    from babelfishr.ui.uninstall_window import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
