"""Coletto (2022) zone sub-models (BuildSpec §14 M2): PHZ, FTRZ, DCZ.

Each zone module is pure (no `config`/`engine` imports, mirrors `core/model.py`
and `core/thermo.py`'s purity constraint) and independently unit-tested against
the paper's reported figures before being wired into `core/model.py` (M2 Phase
4 — the tray-by-tray fixed-point sweep).
"""
