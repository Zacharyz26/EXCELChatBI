"""Each supported entry imports in a fresh interpreter, without preload masking."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module,symbol",
    [
        ("packages.orchestration.analysis_requirements", "explicit_analysis_requirements"),
        ("packages.session.coref", "ReferenceResolver"),
        ("packages.session.execution_context", "resolve_execution_context"),
    ],
)
def test_r4_public_entry_imports_without_host_preload(tmp_path, module, symbol):
    env = dict(os.environ)
    for key, basename in [
        ("CHAT_DB_PATH", "isolated.sqlite3"),
        ("DATASET_DIR", "datasets"),
        ("UPLOAD_DIR", "uploads"),
        ("REPORT_DIR", "reports"),
    ]:
        env[key] = str(tmp_path / basename)
    program = (
        "import importlib,socket,sys\n"
        'def denied(*a,**k): raise AssertionError("no external I/O during fresh import")\n'
        "socket.socket.connect=denied\n"
        "socket.socket.connect_ex=denied\n"
        "socket.create_connection=denied\n"
        "module=importlib.import_module(sys.argv[1])\n"
        "assert hasattr(module,sys.argv[2])\n"
        'print("FRESH_IMPORT_OK")\n'
    )
    result = subprocess.run(
        [sys.executable, "-c", program, module, symbol],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    witness = {
        "entry": module,
        "symbol": symbol,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "layer": "fresh process package import; no Host preload, model, or real database",
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert result.returncode == 0, witness
    assert result.stdout.strip() == "FRESH_IMPORT_OK", witness
