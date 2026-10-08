# Rollback, and the release where there is no such thing

Argo CD will roll any Application back. It reverts the manifests and the
Deployment rolls to the previous image. Whether that returns the service to
working depends entirely on what the migration in that release did, and the
cluster has no opinion about it.

So each service declares it, in `envs/<env>/<service>.yaml`:

| `migration.strategy` | what it does | reverting the commit |
|---|---|---|
| `none` | nothing runs | works |
| `expand` | adds a column, an index, a table. The previous image ignores it | works. The added object stays and is harmless |
| `contract` | removes something the previous image reads | does not work. The pods start, pass liveness, and fail every query that touches it |

The chart renders the consequence onto the Deployment:

```yaml
deploy.sam-farooq.dev/rollback: manual
deploy.sam-farooq.dev/rollback-reason: "contract migration is not reversible"
```

and `argocd/applicationsets/services.yaml` gives that element
`autoSync: false`. An automated sync with self-heal would run an irreversible
migration the moment the commit landed, at whatever hour it landed.

Right now that is `hybrid-search` in both environments: it drops
`lexical_vector_v1` and its index. Whether the image that read that column is
far enough behind is the thing this repo cannot check, which is what the
pre-flight list below is for.

## Before syncing a contract migration

1. **The code that read it has been live for at least one release.** Not
   merged. Live, in prod, with no rollback since. `contract` is the second half
   of a pair and the first half is a release that shipped on its own.
2. **Check it is actually unread.** `pg_stat_user_indexes` for the index and a
   log search for the column name. A query path nobody exercises in staging is
   exactly what this step is for, and staging passing is not evidence.
3. **Note the RDS snapshot you would restore from.** `eks-infra` sets
   `skip_final_snapshot = false` with seven day retention. A restore creates a
   new instance and a new endpoint; it is not an in-place undo.
4. **Sync it by hand and watch the Job.** Wave 1 holds the rollout until the
   Job reports Complete. If the Job fails, wave 2 never starts and the old
   pods are still serving, which is the good outcome.

## When 0.9.1 is bad and the column is already gone

Reverting the commit is the wrong move. It puts `0.8.2` back against a schema
`0.8.2` cannot read, which converts a partial failure into a total one.

The path is forward:

1. **Stop the bleeding without changing the schema.** If the failure is in one
   code path, the fastest fix is a config change: `envs/prod/hybrid-search.yaml`
   carries `RRF_K` and the BM25 parameters, and a ConfigMap change rolls the
   pods because of the checksum annotation on the pod template. No new image,
   no migration.
2. **If the image is wrong, fix forward to `0.9.2`.** The schema stays as it
   is. One commit, automated sync is off for this Application, so a human syncs
   it.
3. **If the schema is wrong, re-add it as an expand migration.** A new
   migration, nullable, no backfill, plus an image that tolerates it being
   empty. Not a revert of the old migration: the completed Job for that content
   hash is still in the namespace, so editing the migration back to its earlier
   content produces a Job name that already exists and it does not run.
4. **If the data mattered, it is a restore.** Step 3 gets the column back
   empty. Nothing in this repo or the cluster has the old values. That is what
   step 3 of the pre-flight list is for.

## The Job name, which is the thing people trip over

```
rag-agent-migrate-4f1c2ba9e07d
                  ^ first 12 of sha256 over the migration directory
```

A Job's pod template is immutable, so re-applying an edited Job under the same
name fails the sync with `field is immutable` and nothing in the message
mentions migrations. The hash in the name means an edited migration is a new
object, and Argo CD prunes the old one.

The cost, stated plainly: edit a migration back to earlier content and the
completed Job for that hash is still there, so it does not run. **Reverting a
migration means writing a new migration.** There is no path where going
backwards in git goes backwards in the database.

## Why the Job is not a PreSync hook

A `PreSync` hook runs on every sync, including a self-heal that changed
nothing, and `hook-delete-policy: BeforeHookCreation` throws away the previous
run's logs. A plain resource at sync wave 1 with a content-hashed name runs
once per migration change and leaves the record in the namespace.

The hook form is better if migrations are idempotent and cheap. These are not
cheap: `rag-agent`'s builds an index.

## What must never be set on these Jobs

`ttlSecondsAfterFinished`. The TTL controller deletes the finished Job, Argo CD
finds it missing from the live cluster on the next sync, re-creates it, and the
migration runs a second time. On an `expand` migration that is a wasted index
build. On a `contract` migration it is a second DROP, which succeeds or fails
depending on whether it was written with `IF EXISTS`.

`check_jobs` in `gitopsvalidate/rules.py` fails the build if it appears.
