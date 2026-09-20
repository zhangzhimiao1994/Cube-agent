# Runtime File Read Policy

`workspace.read`, `workspace_read`, and `read_context(path=...)` share a run-scoped
reader. The runtime resolves authorization from the persisted run, not from tool
arguments or a tool envelope's sandbox label.

## Authorized Sources

- Relative project paths resolve under the persisted tenant/project/session.
- Attachment paths use `<tenant_uuid>/<attachment_id>.bin`, the attachment's
  metadata/manifest, or `<tenant_uuid>/<attachment_id>/<extracted_member>`.
  The attachment ID must be in that run's persisted `attachment_ids`.
- The stored sandbox profile must permit reads and explicitly include
  `workspace.read` in `requested_permissions`. Missing or mismatched run records,
  absent permission, and unsupported scope configurations fail closed.
- Authorization is fetched on every read. A committed revocation applies to the
  next call, even when it reuses an existing gateway instance.
- Query-only `read_context` performs no file read and retains its existing behavior.

The reader does not fall back to the global attachment directory. It does not
change shared gateway roots for individual requests. Returned content remains
bounded to 64 KiB, with truncation reported explicitly.

## Filesystem Boundaries

The runtime reader rejects traversal, special files, hardlinks, and links between
scopes. On POSIX it holds directory descriptors and opens children relative to
those descriptors without following links. On Windows local volumes it holds
directory/file handles without write/delete sharing and rejects reparse points.
Windows network/device namespaces and unsupported platforms fail explicitly.

Project workspace store scope directories also reject symlinks/junctions and
resolution to a different scope. This does **not** make every store/API operation
atomic against an untrusted host process changing directories: metadata and
download responses still include path-based opens. Keep store directories out of
untrusted writable mounts. Binding store downloads/writes to handles throughout
their lifetime remains separate hardening work.

Reading a file is not evidence that its text was injected into a model request,
that a model followed it, or that planning preceded implementation. Those claims
require separate runtime provenance and must not be inferred from this policy.
