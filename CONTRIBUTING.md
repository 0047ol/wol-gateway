# Contributing

By submitting a contribution, you agree that it is distributed under the project license, GNU AGPL-3.0-or-later.

When modifying covered source, add a prominent notice describing the change and its date to the changed file or to a changelog shipped with that source version. Keep existing author, copyright, and license notices. Distribute the complete corresponding source under AGPL terms when required by the license, including for modified versions offered as a network service.

Do not include production credentials, databases, private keys, device identifiers, deployment logs, build output, or prebuilt release binaries in source changes. Use example values and update tests or build documentation when changing a component protocol.

## Release workflow

Keep `README.md` in English as the default GitHub landing page. Put Simplified Chinese documentation in `README.zh-CN.md`; each page must use screenshots in its own language. For every completed code or artifact change, update the relevant record, commit and sync the current public source to Git, and publish the latest website deployment source, firmware package, and Windows service executable as GitHub Release assets when they are safe to distribute. Build a public website package with `tools/package_website_public.py`; do not upload the raw production website download. Before publishing, verify versions, archive integrity, checksums, and scan all source and binary assets for credentials, private keys, databases, internal paths, and server addresses. Do not upload an artifact that contains data explicitly designated private; make its endpoint configurable and rebuild it first.
