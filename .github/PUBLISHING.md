# Publishing Arc Packages to PyPI

## Architecture

Each package has its own workflow file and GitHub environment.
This is required because PyPI trusted publishing needs a unique
(owner, repo, workflow_name, environment) tuple per project.

```
.github/workflows/
├── ci.yml                    # Test on push/PR
├── publish-arcllm.yml        # release + manual
├── publish-arcrun.yml
├── publish-arcagent.yml      # blocked until PEP 541 approved
├── publish-arccli.yml
├── publish-arcteam.yml
├── publish-arcstack.yml
├── publish-arcui.yml         # manual only (placeholder)
├── publish-arcmodel.yml      # manual only (placeholder)
├── publish-arcskill.yml      # manual only (placeholder)
└── publish-arctui.yml        # manual only (placeholder)
```

## Publishing

**On release:** Active packages trigger on `release: published` AND `workflow_dispatch`.
Run them in dependency order: arcllm -> arcrun -> arcagent -> arccli/arcteam -> arcstack.

**Placeholders:** Manual dispatch only. Run once to claim names.

## Dependency Order

```
arcllm (standalone)
  └── arcrun (depends on arcllm)
       └── arcagent (depends on arcllm + arcrun)
            └── arccli (depends on arcagent + arcteam)
                 └── arcstack (depends on arccli)

arcteam (standalone — depends on pydantic only)
```
