# Synthetic evaluation fixture

Everything here is synthetic. `make_snapshot.py` rebuilds `cases/derived-index/snapshot.bundle` with a
fixed identity and fixed dates, so its commit ids are reproducible. `oracles/` sits outside the case
folder on purpose. The two variants in `plan.toml` are fake engines: they test control flow only.

The snapshot includes `fake_variant.py`, copied from `variants/fake_variant.py` by the generator.
The plan launches that workspace copy; it does not pass an external script path to Python. Rebuild
the bundle after changing the fake engine. Test recording destinations are created only after launch.
