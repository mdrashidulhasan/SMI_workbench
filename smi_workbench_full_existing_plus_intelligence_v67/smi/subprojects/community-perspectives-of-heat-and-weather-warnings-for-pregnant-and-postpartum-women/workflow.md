# SMI Workflow

## Workflow Name
Default SMI Literature Intelligence Workflow

## Stages

| Order | Stage | Description | Human Gate |
|---:|---|---|---|
| 1 | Literature Search | Query selected literature databases through API. | No |
| 2 | Deduplication | Remove duplicate citation records. | No |
| 3 | AI Screening | Apply exclusion criteria to citation records. | Yes: pre-ai-screening |
| 4 | Screened Record Retrieval | Retrieve screened records for review. | Yes: post-ai-screening |
| 5 | Summary | Generate a Markdown summary of outputs. | Yes: pre-final-summary |
| 6 | Archive | Copy completed run to archive. | Yes: final-approval |
