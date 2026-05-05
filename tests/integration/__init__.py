"""tests/integration/ — slow-marked end-to-end smoke tests for v0.5.0+.

These tests spawn real subprocesses (the popolad daemon, the popola
CLI, bash) and wire them together with an isolated `$POPOLA_HOME`,
so they live behind ``@pytest.mark.slow`` and stay out of the default
lane. The default-lane gate runs against the unit + integration tests
under ``tests/cli/`` / ``tests/daemon/`` / etc.; this directory
specifically validates that the **release-shipping** scripts
(``examples/quickstart.sh``, future installer one-liners) run
end-to-end without regressions.
"""
