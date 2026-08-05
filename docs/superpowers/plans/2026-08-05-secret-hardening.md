# Secret Rotation and Storage Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe AES key rotation, tenant-local stable fingerprints, bounded hostile-input handling, sanitized persistence failures, and an ORM-free repository boundary.

**Architecture:** `SecretCipher` owns an active AES key, a validated decryption keyring, and stable 32-byte fingerprint root material. The root always passes through a domain-separated KDF before HMAC use. Fingerprints bind a versioned, length-prefixed tenant context; `SecretRepository` translates ORM rows into immutable redacted records; `SecretService` sanitizes SQLAlchemy failures after rollback.

**Tech Stack:** Python 3.12, cryptography AESGCM/HMAC-SHA256, SQLAlchemy asyncio/PostgreSQL, pytest, Ruff, mypy.

---

### Task 1: Rotation and tenant-local fingerprints

**Files:**
- Modify: `src/agent_hub/security/secrets.py`
- Modify: `tests/unit/security/test_secrets.py`
- Modify: `tests/integration/security/test_secret_store.py`

- [x] **Step 1: Write failing unit and PostgreSQL tests**

Add tests constructing `SecretCipher(key2, key_id="v2", decryption_keys={"v1": key1}, fingerprint_key=stable_key)`, asserting v1 decryptability, v2 sealing, stable same-tenant fingerprints across rotation, and distinct cross-tenant fingerprints. Add a PostgreSQL rotation test proving duplicate lookup returns the v1 reference and new values use v2.

- [x] **Step 2: Verify RED**

Run: `.venv\\Scripts\\python -m pytest tests/unit/security/test_secrets.py tests/integration/security/test_secret_store.py -q`

Expected: constructor rejects the new arguments and cross-tenant fingerprints are currently equal.

- [x] **Step 3: Implement the minimal keyring and fingerprint scheme**

Validate every AES key and fingerprint root as exactly 32 bytes and every key id with the existing safe pattern. Build a keyring containing the active key plus non-conflicting decryption keys. A first single-key deployment defaults the fingerprint root to its master key. Any configuration with retired decryption keys must explicitly pass the stable initial or independent fingerprint root. Both default and explicit roots pass through `_FINGERPRINT_KEY_LABEL` domain derivation before HMAC use. Compute HMAC over `agent-hub-secret-fingerprint-v2`, a four-byte context length, context bytes, and plaintext; select AES keys by `sealed.key_id` during open.

- [x] **Step 4: Verify GREEN**

Run the Task 1 target tests and require zero failures.

**Pre-release fingerprint v2 decision:** Scheme v2 was introduced before the first Task 5 release. Remote `main` remained at `d923fcd`, where neither `SecretRow` nor encrypted-secret persistence existed, so there are no production or target-environment rows using the earlier experimental fingerprint algorithm. The `0004_encrypted_secrets` schema does not automatically make old fingerprint values compatible. Any experimental environment populated from commit `9544b6a` must either clear those secret rows or decrypt them with the old cipher and write them again using scheme v2 before upgrading.

### Task 2: Bounded hostile inputs

**Files:**
- Modify: `src/agent_hub/security/secrets.py`
- Modify: `tests/unit/security/test_secrets.py`

- [x] **Step 1: Write failing boundary and decode-spy tests**

Cover exact 65,536-byte acceptance, ASCII/multibyte/whitespace over-limit rejection, oversized nonce/fingerprint/ciphertext/key_id fields, and monkeypatch base64/AES operations to assert oversized values fail before decoding or decryption.

- [x] **Step 2: Verify RED**

Run the hostile-input unit selection and confirm oversized whitespace or encoded fields reach expensive operations today.

- [x] **Step 3: Implement preflight limits**

Check string character length before `strip()`/UTF-8 encoding. Before base64 work, validate field types, nonce length 16, fingerprint length/hex, key id length, ciphertext encoded length 24..87,404; after decoding enforce ciphertext length 16..65,552. Bound context UTF-8 bytes to 1,024.

- [x] **Step 4: Verify GREEN**

Run all security unit tests and require zero failures.

### Task 3: Sanitized persistence boundary

**Files:**
- Modify: `src/agent_hub/db/session.py`
- Modify: `src/agent_hub/security/secrets.py`
- Modify: `src/agent_hub/security/__init__.py`
- Modify: `tests/unit/test_database_resources.py`
- Modify: `tests/integration/security/test_secret_store.py`

- [x] **Step 1: Write failing runtime and PostgreSQL tests**

Assert runtime engines set `hide_parameters=True`. Trigger a foreign-key insert failure via `SecretService.create_or_get`, then inspect exception string/repr/formatted traceback for absence of plaintext, fingerprint, nonce, and ciphertext; assert the service exposes only `SecretPersistenceError("secret could not be stored")` and no row remains.

- [x] **Step 2: Verify RED**

Run the database-resource and persistence tests; expect parameter hiding to be false and a SQLAlchemy exception to escape.

- [x] **Step 3: Implement the minimal sanitization**

Pass `hide_parameters=True` in `build_engine`. Catch `SQLAlchemyError` only around the transaction in a private/result boundary, let the context manager roll back, exit the active exception stack, then have the public method raise `SecretPersistenceError("secret could not be stored")` with no cause or context. Keep plaintext validation outside the transaction try block.

- [x] **Step 4: Verify GREEN**

Run Task 3 tests and require zero failures.

### Task 4: ORM-free repository boundary

**Files:**
- Modify: `src/agent_hub/security/secrets.py`
- Modify: `tests/unit/security/test_secrets.py`

- [x] **Step 1: Write a failing repository materialization test**

Assert `SecretRepository.get` returns an immutable `repr=False` internal record or `SealedSecret`-bearing value rather than `SecretRow`, and assert its repr contains no nonce, fingerprint, or ciphertext.

- [x] **Step 2: Verify RED**

Run the repository boundary test; expect the current `SecretRow` result to fail the domain-type assertion.

- [x] **Step 3: Implement internal storage record translation**

Add frozen/slotted/redacted `StoredSecret(secret_id, tenant_id, sealed)` and translate the ORM row inside the repository. Make the service consume `stored.sealed` only.

- [x] **Step 4: Verify GREEN**

Run security unit/integration tests and require zero failures.

### Task 5: Final verification and commit

**Files:**
- Verify all modified files

- [x] **Step 1: Run attack-focused and full verification**

Run security unit/integration tests, full pytest, `ruff check .`, `mypy --strict src tests`, explicit test-database `alembic current` and `alembic check`, and `git diff --check`.

- [x] **Step 2: Review staged scope**

Stage only the plan, security implementation/tests, session engine change, and database-resource test. Run `git diff --cached --check` and inspect the staged stat.

- [x] **Step 3: Commit**

Run: `git commit -m "fix: harden secret rotation and storage"`
