# GitHub Actions workflows

These files are kept outside `.github/` because pushing workflow files requires
the `workflows` permission on the GitHub App token. To enable them, copy them into
place and commit from an account that has that permission:

```bash
mkdir -p .github/workflows
cp azure/github-actions/ci.yml .github/workflows/ci.yml
cp azure/github-actions/azure-deploy.yml .github/workflows/azure-deploy.yml
git add .github/workflows && git commit -m "Enable CI and Azure deploy workflows"
```

* `ci.yml` — lint + the full test suite on every push and pull request. The suite
  includes a full end-to-end crawl against a local fixture server, so CI needs no
  Playwright browser download.
* `azure-deploy.yml` — scheduled (06:30 UTC) or manual: builds and pushes the image,
  starts the Azure Container Instance, waits for it to finish, and fails the run if
  no dataset was published. Requires repository variables `AZURE_RESOURCE_GROUP`,
  `AZURE_CONTAINER_REGISTRY`, `AZURE_STORAGE_ACCOUNT` and the `AZURE_CREDENTIALS`
  secret in an environment named `azure`.
