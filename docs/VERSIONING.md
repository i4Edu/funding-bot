# Versioning strategy

Funding Bot uses semantic versioning: `MAJOR.MINOR.PATCH`.

## Rules

- **MAJOR** — breaking API, workflow, schema, or deployment changes that require operator action.
- **MINOR** — backwards-compatible features such as new connectors, dashboard capabilities, or deployment manifests.
- **PATCH** — backwards-compatible bug fixes, security fixes, and documentation-only corrections.

## Release conventions

- Git tags use a `v` prefix, for example `v1.0.0`.
- Pre-release candidates use suffixes such as `v1.1.0-rc.1`.
- Production container images should be pinned to an immutable version tag or image digest; avoid `latest` in Kubernetes manifests.

## Deployment compatibility

- Keep Kubernetes manifests aligned with the application release they deploy.
- When changing env vars, probes, resource requirements, or autoscaling thresholds, update `docs/KUBERNETES.md` and `docs/DEPLOYMENT.md` in the same change.
- Database or data-shape changes should ship with a migration plan and release notes describing rollback expectations.

## Recommended release flow

1. Merge changes into the default branch.
2. Cut a version tag such as `v1.0.1`.
3. Publish a matching container image tag.
4. Update Kubernetes manifests to use the new image tag or digest.
5. Record any operational changes in deployment notes before rollout.

## Roadmap alignment

The milestone history in `README.md` and `roadmap.md` describes product scope. Version numbers should continue to map to shipped capability sets, while patch releases remain reserved for safe fixes that do not expand scope.
