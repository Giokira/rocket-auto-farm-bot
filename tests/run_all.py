"""Esegue tutti gli smoke test in sequenza. Uso: python tests/run_all.py"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = ["smoke_db.py", "smoke_app.py", "smoke_flow.py", "smoke_paypal.py",
          "smoke_license_server.py"]

failed = []
for suite in SUITES:
    print(f"\n=== {suite} ===")
    result = subprocess.run([sys.executable, str(HERE / suite)], cwd=HERE)
    if result.returncode != 0:
        failed.append(suite)

print("\n" + "=" * 40)
if failed:
    print("FALLITI:", ", ".join(failed))
    sys.exit(1)
print("TUTTE LE SUITE PASSATE")
