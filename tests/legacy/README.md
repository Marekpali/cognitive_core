Exact copies of production code, used only by tests/test_rollback_d1_on_d2.py
to prove that rolling the code back to D1 is safe on a D2-migrated database.

- storage_d1.py = `d1-deployed:src/storage.py`, SHA256 6bb9c184420a45405d4b586a4b6964b2e912e049e01381080522e76d13bd3669
- core_d1.py    = `d1-deployed:src/core.py`,    SHA256 c3a70399c9faab32956ffa837921c197b305ee1ae4e59010e55c72e4141fabb1

Both hashes equal the D1 production attestation (docs/D1-DEPLOYMENT.md).
Stored byte-exact (`-text` in .gitattributes); never edit.
