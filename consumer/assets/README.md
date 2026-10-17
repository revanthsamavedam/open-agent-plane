# Vendored SQLite WASM assets

The consumer app runs real SQLite in the browser via **sql.js 1.10.3**
(SQLite compiled to WebAssembly).

| File | SHA-256 |
|---|---|
| `sql-wasm.js` (49,857 B) | `558a72c3ab3415d0e6d243cfd23f9d61543600d59054b4b7b8da3cd65f6b9fd4` |
| `sql-wasm.wasm` (655,300 B) | `d7e61b828523001f26ce0b3f88dabcf6c12e5e6edf80eb4f08b26ac7b946ff88` |

`sql-wasm.js` is committed here. `sql-wasm.wasm` is a binary and is not
stored in this git repo (the push tooling used for this project only
carries UTF-8 text). If you cloned the repo and the `.wasm` file is
missing, fetch the stock binary once:

```bash
bash consumer/tools/fetch-wasm.sh
```

It downloads the exact sql.js 1.10.3 binary and verifies its SHA-256
against the table above before placing it here. The hosted deployment
serves the real binary — this only affects fresh clones.
