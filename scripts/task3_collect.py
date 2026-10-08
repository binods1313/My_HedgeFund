import subprocess
import sys
import os

files = [
    'tests/test_demo_verification.py',
    'tests/test_secret_scan.py',
    'tests/test_phase0_bugs.py',
    'tests/test_engine/test_equity.py',
    'tests/test_engine/test_executor_commands.py',
    'tests/test_engine/test_gates.py',
    'tests/test_engine/test_gateway.py',
    'tests/test_engine/test_guard.py',
    'tests/test_engine/test_indicators.py',
    'tests/test_engine/test_llm_client.py',
    'tests/test_engine/test_risk.py',
    'tests/test_engine/test_risk_wiring.py',
    'tests/test_engine/test_state.py',
    'tests/test_engine/test_strategy_dryrun.py',
    'tests/test_engine/test_telegram_service.py',
    'tests/test_engine/test_telemetry.py',
    'tests/test_execution_guards.py',
    'tests/test_formatting_and_mode.py',
    'tests/test_llm_validator.py',
    'tests/test_metrics.py',
]

outdir = r'C:\Users\Binod\AppData\Local\Temp\kilo'
os.makedirs(outdir, exist_ok=True)

for f in files:
    r = subprocess.run(
        [sys.executable, '-m', 'pytest', f, '-q', '--tb=short'],
        capture_output=True,
        text=True,
        cwd=r'E:\HedgeFund_Binod_v2',
    )
    out = r.stdout + r.stderr
    safe = f.replace('/', '_').replace('.', '_')
    path = os.path.join(outdir, f'task3_{safe}.txt')
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write(out)
    print(f'=== {f} ===')
    print(out[:1000])
    print()
