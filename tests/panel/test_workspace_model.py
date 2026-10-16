"""Presentation claims checked against a fresh run of the real simulated workflow."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_workspace_action_claims_against_integrated_recording(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for frontend presentation checks')
    # Load the existing offline recorder without requiring scripts as an installed package.
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('explorer_recorder', root / 'scripts/build_explorer.py')
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)
    recording = tmp_path / 'recording.json'
    recording.write_text(json.dumps(recorder.record_replay()))
    result = subprocess.run([node, '--test', str(root / 'tests/panel/workspace_model.test.cjs')],
                            env={**os.environ, 'TRANSITOS_UI_RECORDING': str(recording)},
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
