# Releasing

The gateway uses independent `0.x.y` preview versions. A release names one exact
source commit and publishes `ghcr.io/pythc/chatgpt-web-gateway:v<version>` for
Linux amd64 and arm64. There is no moving `latest` tag during preview. Prefer an
image digest when reproducibility matters.

## Maintainer procedure

1. Update `version.txt`, the changelog and `docs/releases/<version>.md` in a PR.
   State whether actual model qualification is new or inherited from a baseline.
2. Run docs checks, offline suites and image packaging/startup checks; review the
   full diff, permissions and migration/rollback implications. Merge passing CI.
3. On the merged `main` commit, create and push an annotated `v<version>` tag.
   Do not move a published tag or reuse a version for a changed image.
4. The tag workflow verifies the tag matches `version.txt` and belongs to `main`,
   runs offline checks, builds both architectures, tests each published digest,
   then creates a GitHub prerelease with the digest and release-specific notes.
5. On first publication, make the linked GHCR package public in its package
   settings; verify an anonymous pull. Public source does not guarantee public
   package visibility. Verify the release's tag, source commit and workflow.

CI uses synthetic data, never a ChatGPT account. Inference acceptance is a separate
opt-in procedure. Actions are pinned; release write permissions are limited to
its publication job. Fork workflows never publish into the upstream namespace.

## Failure and retry

A build failure before push creates no image. A failure after push can leave an
image without a Release; do not advertise it as a qualified release. Inspect the
failed step first. After an interrupted post-push qualification, dispatch the publication workflow
from `main` with the existing tag and exact index digest. This recovery path
verifies the tag still resolves to that digest, tests each architecture by its
child manifest digest, and skips rebuilding/pushing. It must not replace an
already published release. The workflow
refuses to start publication if the GitHub Release already exists. If published
content must change, use a new patch version. Never delete private runtime state
or restart a user's deployment as part of publication.

## Install a published image

After preparing `.env` and `data/token.txt` as in the README:

```sh
docker compose -f compose.yaml -f compose.release.yaml pull gateway
docker compose -f compose.yaml -f compose.release.yaml up -d --no-build gateway
```

`compose.release.yaml` pins the preview tag. For another release, set
`GATEWAY_IMAGE=ghcr.io/pythc/chatgpt-web-gateway:v0.1.0` in your private `.env`,
or use `ghcr.io/pythc/chatgpt-web-gateway@sha256:<digest-from-release>`.
Pulling only downloads the image; `up` replaces the running container.

## Upgrade and rollback

Back up private `.env` and `data/` before upgrading; retain the old image digest
and Compose files. Keep private backups outside the checkout/build context.
Record a completed synthetic client check as well as container startup.

To upgrade, change `GATEWAY_IMAGE` to the desired release/digest, pull, then run
`up -d --no-build`. For rollback, restore the previous image reference and run
`up -d --no-build` again with the same preserved mounts. If a future version
migrates state, follow its release-specific restoration instructions before
starting an older image. The first release introduces no new state migration.
Older pre-file-configuration images restore their old account-page behavior.
Never assume old images understand JSON credential formats; see
[configuration](configuration.md#reloading-and-upgrading).
