# Contributing

Open an issue before a large behavior or hardware change. Keep device operations fail-closed, never add real subscriber data to fixtures, and preserve upstream attribution.

## NAS compatibility reports

Use the **NAS hardware compatibility** Issue form for a new model or system build. After the report
is reproduced or clearly marked as unverified, add one record under `drivers/catalog/` and run
`python3 tools/validate_nas_catalog.py`. Follow `drivers/README.md`: remove all device and subscriber
identifiers, and do not submit third-party kernel binaries in a pull request. A compatibility report
only becomes `release-ready` after maintainers reproduce the build and publish a checksummed driver
asset from an auditable source and toolchain.

## Branches

`develop` is the integration branch. Create a focused `feat/*` or `fix/*` branch from its latest
commit and open the pull request back to `develop`. Changes collect there, get reviewed and are
released together; do not merge each completed feature separately to `main`.

`main` describes what has actually been published and takes only release merges. Prepare a release
on `release/vX.Y.Z` from `develop`, merge the latest `main` into that branch, and update `VERSION`,
the WebUI package versions, `CHANGELOG.md`, the bilingual release notes, and the release
classification in `update-policy.json`. Keep the installation channels on their last accepted
version until the new release passes the real-device checks in `docs/RELEASE_CHECKLIST.md`.

Open the release pull request against `main`. After its CI passes and it is merged, add a signed
`vX.Y.Z` tag to the merge commit; that tag builds and publishes the release images. Verify the
Release assets, checksums, and multi-architecture manifest, then merge `main` back into `develop`
so the next change starts from the exact published history. Promote the update channels separately
after ARM64 and amd64 acceptance.

An urgent production fix may branch from `main` only when unreleased work on `develop` must not ship
with it. Treat that hotfix as a release: test it in a pull request to `main`, version and tag it, and
immediately merge the published result back into `develop`. Ordinary fixes still go through
`develop`.

CI runs for pull requests targeting `develop` or `main`, and once more after a merge lands on either
protected branch. A branch push does not run the same matrix before its pull request, and a tag push
does not start ordinary CI; `v*` tags use the dedicated Release workflow. Superseded runs for the
same pull request are cancelled automatically.

Enable the hooks once per clone:

```bash
git config core.hooksPath hooks
```

`hooks/pre-push` refuses to publish a subscriber identifier it cannot recognise as fictional.
CI runs the same check, but a push cannot be taken back — and it scans the commits being
pushed rather than the working tree, because a value that was committed and then removed is
still in the history the push publishes.

Before submitting a change:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile control/app/*.py engine/*.py host/*.py
sh tools/check-subscriber-identifiers.sh
cd webui && npm ci && npm run build
```

Use focused commits, document user-visible changes in `CHANGELOG.md`, and add tests for routing, authentication, device state and secret redaction.
