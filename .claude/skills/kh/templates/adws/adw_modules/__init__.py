import os
if os.name == "nt":
    raise SystemExit("Native Windows is not supported. Use WSL2 (not WSL1) with Ubuntu; run KH and install.py inside Ubuntu, with the project under ~/ (not /mnt/c).")
