# Repository agent instructions

These instructions apply to the entire repository.

## Definition of done

An implementation change is not complete when the code merely compiles or its
unit tests pass. Unless the user explicitly requests a local-only change or
forbids publication, every completed fix or feature must finish with all of the
following:

1. Verify the behavior as the user experiences it, using the available agentic
   capabilities to inspect runtime state, exercise relevant integration paths,
   review logs, and test failure/recovery cases. Do not claim that a feature is
   fully working from code inspection alone.
2. Add or update regression tests that cover the intended behavior and the
   failure that motivated the change.
3. Run the complete relevant test, lint, metadata, and packaging checks. At a
   minimum, run the Python suite, Rust tests and Clippy, AppStream validation,
   and package inspection. Record any environment limitation honestly.
4. Update the project version using semantic versioning:
   - patch for compatible fixes and refinements;
   - minor for backward-compatible new features;
   - major for breaking compatibility changes.
5. Update every version-bearing source consistently, including
   `mwb_linux/__init__.py`, `portal-bridge/Cargo.toml`, the project entry in
   `portal-bridge/Cargo.lock`, current package names in `README.md`, AppStream
   release metadata, RPM changelog metadata, and version-sensitive tests.
6. Update `RELEASE_NOTES.md` with a concise, polished, user-focused release
   title and notes. Explain what changed, why it matters, how it was verified,
   any important limitations, and the available package formats. Do not use a
   raw commit list as the release description.
7. Commit the complete release scope, push it to the default branch, create and
   push the matching annotated `vX.Y.Z` tag, wait for the release workflow, and
   verify that the public GitHub release and all expected assets are present.

Never reuse or move an already-published version tag. If publication fails,
repair the failure and finish the same release before declaring the task done.

## Visual changes

For any user-visible visual change, launch the affected interface and inspect
it through the available browser/desktop preview tooling at representative
sizes and states. Save a clean screenshot under `docs/releases/vX.Y.Z/`, add a
regression check where practical, and include the screenshot in the GitHub
release notes or upload it as a release asset. Do not reuse an older screenshot
as evidence for a new visual change.

If a release contains no visual change, state that in the verification notes;
no screenshot is required.

## Release safety

- Inspect `git status` and the complete diff before staging. Do not include
  unrelated user changes.
- Never publish with failing checks, mismatched versions, missing artifacts, or
  an unverified release page.
- The release tag must point to the exact tested commit on the default branch.
- Confirm the release is marked latest and includes `SHA256SUMS`, DEB, RPM, and
  AppImage assets for both x86-64 and ARM64.
