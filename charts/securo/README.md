# Securo Helm Chart

[Securo](https://github.com/securo-finance/securo) is an advanced, self-hosted personal finance manager with built-in AI agents. This Helm chart provides a complete, production-ready deployment of Securo to any Kubernetes cluster.

## Features

- **Complete Application Stack**: Deploys the Next.js React frontend and FastAPI backend, fully supporting all features including Open Banking integrations, OIDC authentication, and AI financial agents.
- **Asynchronous Task Scheduler**: Runs Celery workers and a Celery beat singleton for automated background processing (such as bank syncs, recurring transactions, and exchange rate updates).
- **Databases**: Easily configure connections to your external PostgreSQL and Redis instances.
- **Gateway API & Ingress**: Native support for standard Kubernetes Ingress or the modern Kubernetes Gateway API (`HTTPRoute`).

## Prerequisites

- Kubernetes 1.25+
- Helm 3.2.0+
- PostgreSQL 15+ (with the `pgvector` extension installed)
- Redis 7+
- Persistent Volume (PV) provisioner support in the underlying infrastructure

### Persistent Storage Requirements

The following shared-file requirement applies to **local storage**. With
`config.storageProvider: s3`, attachments and invoice logos use object storage;
you can disable `persistence.attachments.enabled` after migrating existing files.
AI knowledge documents and embedding caches still need their configured volumes.

Since the Backend, Celery Worker, and MCP Server all share the same files (like uploaded attachments and AI knowledge bases), they all mount the same Persistent Volume Claims concurrently.
**If your cluster spans multiple nodes, you MUST use a StorageClass that supports `ReadWriteMany` (RWX) access mode (e.g., NFS, CephFS, or Longhorn RWX).**
If your storage only supports `ReadWriteOnce` (RWO), you must restrict all Securo pods to run on a single node (using `nodeSelector` or `podAffinity`) but that is an antipattern in Kubernetes.

## Quickstart

If you want to install the latest official release from the GitHub Container Registry (GHCR):

```bash
helm install securo oci://ghcr.io/securo-finance/charts/securo --version <VERSION>
```

If you are developing locally and want to install from the source repository:

```bash
helm install securo ./charts/securo
```

## Configuration

All application-level features (such as AI Services, OIDC authentication, and Open Banking integrations) can be configured directly through Helm.
The keys in the `config:` and `secret:` blocks of the `values.yaml` map directly to the `UPPER_SNAKE_CASE` environment variables used in the standard Docker setup (converted to `camelCase`).

For a full list of available environment variables and API keys, please refer to the **[Securo Application Configuration Guide](https://github.com/securo-finance/securo-docs)**.

### Secrets Management (Production)

For production deployments, we highly recommend managing your secrets securely by passing an existing Kubernetes `Secret` rather than putting plain text keys in your `values.yaml`:

```yaml
global:
  existingSecret: "my-securo-secrets"
```

The existing Secret must contain environment-variable keys (e.g., `SECRET_KEY`,
`DATABASE_URL`, `AGENTS_OPENAI_API_KEY`). The camelCase names in `values.yaml`
are converted to these names only when the chart creates the Secret itself.

### S3 attachment storage

```yaml
config:
  storageProvider: s3
  storageS3Bucket: securo-attachments
  storageS3Region: garage
  storageS3EndpointUrl: https://s3.example.com
  storageS3AddressingStyle: path
persistence:
  attachments:
    enabled: false
global:
  existingSecret: securo-secrets
```

S3 support is part of the regular backend image, so `backend.image.tag` stays
as it is.

In addition to the usual application secrets, provide `STORAGE_S3_ACCESS_KEY`
and `STORAGE_S3_SECRET_KEY` in `securo-secrets` (optionally
`STORAGE_S3_SESSION_TOKEN`). Alternatively, use `secret.storageS3AccessKey`,
`secret.storageS3SecretKey`, and `secret.storageS3SessionToken` when letting Helm
create the Secret. Leave credentials unset when using an SDK-supported workload
identity. For AWS S3, omit the endpoint and use addressing style `auto`.
`http://` endpoints are rejected unless `config.storageS3AllowInsecureHttp: "true"`
is set (development only; traffic is unencrypted).

Create a private bucket before deployment and migrate existing attachments
before disabling their PVC. Consult the repository README's Attachment Storage
section for permissions, migration, and integration testing. S3 mode does not
move AI knowledge files to object storage.

### AI Agents and the MCP server

`mcpServer.enabled` runs the built-in MCP server inside the cluster. It is not added to the Ingress or the HTTPRoute unless you also set `mcpServer.publicRoute: true`, so publishing it to your public entry point is a deliberate step; when you do, set `config.agentsExternalMcpUrl` to the URL clients should use. Tokens are signed with `secret.agentsMcpJwtSecret` when it is set, or with a key derived from `secretKey` otherwise, so a random `secretKey` is enough. A known public value is ignored and the derived key used instead, since anyone holding the signing key could mint a token for any user.

## Upgrading to PostgreSQL 18

This applies only to the bundled database (`postgresql.enabled: true`). Chart
releases after 0.16.6 run it on PostgreSQL 18 instead of 16. A new PostgreSQL
major version cannot read the old one's data files, so the data moves over with
a dump and restore.

The database now lives on a new claim, `postgres-data-<fullname>-postgresql-0`,
mounted at `/var/lib/postgresql`. The old claim, `data-<fullname>-postgresql-0`,
is never written to again and is your rollback. Because the claim changed,
`helm upgrade` refuses to update the existing StatefulSet until you delete it;
deleting a StatefulSet leaves its claims in place.

`<fullname>` is the prefix of the StatefulSet that `kubectl get statefulset`
lists as `<fullname>-postgresql`.

1. Stop everything that writes to the database, then dump it from the
   PostgreSQL 16 pod:

   ```bash
   kubectl scale deployment -l app.kubernetes.io/instance=<release> --replicas=0
   kubectl exec <fullname>-postgresql-0 -- pg_dump -U postgres -Fc securo > securo-pg16.dump
   ```

2. Delete the StatefulSet and upgrade the chart with your usual arguments.
   PostgreSQL 18 starts on the new, empty claim, and the migration job creates
   an empty schema in it:

   ```bash
   kubectl delete statefulset <fullname>-postgresql
   helm upgrade <release> <chart> ...
   ```

3. Stop the app again and replace the empty database with the dump:

   ```bash
   kubectl scale deployment -l app.kubernetes.io/instance=<release> --replicas=0
   kubectl exec <fullname>-postgresql-0 -- psql -U postgres -c 'DROP DATABASE securo WITH (FORCE)' -c 'CREATE DATABASE securo'
   kubectl exec -i <fullname>-postgresql-0 -- pg_restore -U postgres -d securo --exit-on-error --single-transaction < securo-pg16.dump
   kubectl exec <fullname>-postgresql-0 -- psql -U postgres -d securo -c ANALYZE
   ```

4. Run the same `helm upgrade` again. It scales the app back up, and the
   migration job brings the restored schema up to date.

To roll back, delete the StatefulSet again and run
`helm rollback <release> <revision>` with the revision from before the upgrade
(`helm history <release>` lists them); that revision mounts the old claim.
Once you are satisfied, delete the old claim with
`kubectl delete pvc data-<fullname>-postgresql-0`, and the dump file.

## Uninstalling the Chart

To uninstall/delete the `securo` deployment:

```bash
helm uninstall securo
```

This command removes all the Kubernetes components associated with the chart and deletes the release. Note that Persistent Volume Claims (PVCs) created by the chart might not be deleted automatically to prevent accidental data loss.
