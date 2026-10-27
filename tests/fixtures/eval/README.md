# Synthetic evaluation fixture

Everything here is synthetic. `make_snapshot.py` rebuilds `cases/derived-index/snapshot.bundle` with a
fixed identity and fixed dates, so its commit ids are reproducible. `oracles/` sits outside the case
folder on purpose. The two variants in `plan.toml` are fake engines: they test control flow only.
