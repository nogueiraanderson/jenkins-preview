"""Populate the sets registry for the whole suite from the pxb fixture.

Test parametrisation reads SETS at collection time, so the registry must be
loaded here, at conftest import, before any test module is collected. The
fixture file is used directly because collection must not write to disk: a
read-only test environment (a sandboxed reviewer, a hermetic CI) has to be able
to collect the suite.
"""

from pathlib import Path

from jenkins_preview.sets import initialize

PXB_SETS_FILE = str(Path(__file__).parent / "fixtures" / "pxb-sets.json")
initialize(PXB_SETS_FILE)
