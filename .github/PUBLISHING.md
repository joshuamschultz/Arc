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
├── publish-arcagent.yml      # arc-agent on PyPI
├── publish-arccli.yml        # arccmd on PyPI
├── publish-arcteam.yml
├── publish-arcstack.yml
├── publish-arcui.yml         # manual only (placeholder)
├── publish-arcmodel.yml      # manual only (placeholder)
├── publish-arcskill.yml      # manual only (placeholder)
└── publish-arctui.yml        # manual only (placeholder)
```

## Package Name Mapping

| Directory | PyPI Name | Python Import |
|-----------|-----------|---------------|
| `packages/arcagent/` | `arc-agent` | `import arcagent` |
| `packages/arccli/` | `arccmd` | `import arccli` |
| `packages/arcllm/` | `arcllm` | `import arcllm` |
| `packages/arcrun/` | `arcrun` | `import arcrun` |
| `packages/arcteam/` | `arcteam` | `import arcteam` |
| `packages/arcui/` | `arcui` | `import arcui` |
| `packages/arcmas/` | `arcmas` | `import arcmas` |

## Publishing

**On release:** Active packages trigger on `release: published` AND `workflow_dispatch`.
Run them in dependency order: arcllm -> arcrun -> arc-agent -> arccmd/arcteam -> arcmas.

**Placeholders:** Manual dispatch only. Run once to claim names.

## Dependency Order

```
arcllm (standalone)
  └── arcrun (depends on arcllm)
       └── arc-agent (depends on arcllm + arcrun)
            └── arccmd (depends on arc-agent + arcteam)
                 └── arcmas (depends on arccmd)

arcteam (standalone — depends on pydantic only)
```
