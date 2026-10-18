# self-hosted-artifacts

Pass files between jobs of a GitHub Actions run through a folder on your self-hosted runner's
machine, instead of GitHub's artifact storage. Nothing counts toward your GitHub storage, and
handing files over is a local copy.

```yaml
jobs:
  build:
    runs-on: self-hosted
    steps:
      - uses: actions/checkout@v7
      - run: ./gradlew assembleRelease
      - uses: MRezaNasirloo/self-hosted-artifacts/save@v1
        with:
          name: release
          path: |
            app/build/outputs/apk/release/*.apk
            app/build/outputs/bundle/release/*.aab

  publish:
    needs: build
    runs-on: self-hosted
    steps:
      - uses: actions/checkout@v7
      - uses: MRezaNasirloo/self-hosted-artifacts/load@v1
        with:
          name: release
      - run: ls app/build/outputs/apk/release
```

## How it works

- **Save** copies the matching files to `~/self-hosted-artifacts/<owner>_<repo>/<run id>/<name>/`,
  with a checksum of each. A save with the same name in the same run replaces the old one.
- **Load** copies them back to the same paths in the checkout and fails if any file changed.
- **Cleanup:** each save deletes that repository's runs older than `retention-days` (default 2).
  Re-running a failed job works until then.
- The run's summary page says what was saved and where.

## Inputs

| Action | Input | Default | |
|---|---|---|---|
| save | `name` | | Name to load the files by |
| save | `path` | | Patterns, one per line, relative to the checkout. Folders save everything in them. |
| save | `if-no-files-found` | `error` | `ignore` saves nothing instead of failing |
| save | `retention-days` | `2` | Older runs of this repository are deleted on save |
| both | `dir` | `~/self-hosted-artifacts` | Folder on the machine; save and load must match |

## Requirements and limits

- Every runner the run's jobs use must be on **one machine**, since that's where the files are.
- Python 3 on the runner. Nothing else.
- There's no download button on the run page. To get a file, copy it off the machine.
- Repositories get separate folders, but runners on the same machine and user can read each other's.
  Use it for repositories you trust.
- A repository that stops running CI leaves its last runs behind until you delete them.
