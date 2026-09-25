# SuccessFactors Toolkit

[![CI](https://github.com/wudaoyou/successfactors-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/wudaoyou/successfactors-toolkit/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

A self-hosted toolkit for SAP SuccessFactors API troubleshooting, payload
extraction, and integration development, exposed as a REST API and as an MCP
(Model Context Protocol) server.

| API | Protocol | Endpoint prefix |
|-----|----------|-----------------|
| EC SFAPI — Compound Employee | SOAP 1.1 | `/api/sfapi/ce/` |
| OData | REST (v2) | `/api/odata/` |

This is an independent project, not affiliated with or endorsed by SAP SE.
SAP and SuccessFactors are trademarks of SAP SE.

## Start here: Docker MCP → credentials → ask → export

> **Data security: prefer local deployment.** For sensitive SuccessFactors
> employee data and credentials, we recommend running the MCP server in local
> Docker and using a local AI agent, rather than an online AI platform or a
> third-party hosted MCP service. Keep credentials and exports on your machine;
> do not upload private keys or employee payloads to online platforms.
>
> **A local AI agent is not necessarily a local model.** If it calls a cloud
> model, prompts, tool responses, previews, and file contents supplied to that
> model may leave your machine. If HR data must stay within your controlled
> environment, use a locally hosted model and local file-processing tools,
> and check the agent's outbound data handling. Local Docker alone does not
> guarantee this. The toolkit still connects to your configured SuccessFactors
> tenant to query data.

For functional consultants and business key users, start with the
[business user guide](docs/DOCKER_MCP_GUIDE.md) or its
[English/Chinese HTML edition](docs/DOCKER_MCP_GUIDE.html).
Ask IT to complete the one-time Docker Compose setup and provide approved
connection files. Then:

1. Check Docker Desktop or your IT-managed Docker service is running, then open your local AI application.
2. Confirm with IT that the connection files are in `sf-toolkit/credentials`.
3. Confirm the SuccessFactors environment and ask for the employee, date,
   and information you need.
4. Find results under `sf-toolkit/data/mcp`. Ask a file-capable AI application
   for CSV or another supported format, or a copy in an authorized folder.

The guide includes example business questions, completion checks,
troubleshooting, and expandable one-time settings for your administrator.
Original OData results are JSON; Compound Employee results are XML.
CSV conversion requires local file tools in the AI application.

## Documentation

| Page | Covers |
|---|---|
| [REST API](docs/REST_API.md) | Fail-closed setup, install and run, cheat sheet, response format |
| [Connect to SuccessFactors](docs/CONNECT.md) | Key pair generation, environment variables, private key resolution order, tenant management |
| [EC SFAPI (SOAP)](docs/SFAPI.md) | Compound Employee single lookup, structured filter query, pagination, known footguns |
| [OData API](docs/ODATA.md) | `execute` / `extract` / `extract-by-filter-in`, per-request connection override, known API footguns |
| [MCP server](docs/MCP_SERVER.md) | Tools for AI agents, payload handling, export formats, PII tokenization, plugins |
| [Development](docs/DEVELOPMENT.md) | Local dev setup, linting, tests, release process |

## Data handling

Use synthetic examples and test fixtures. Credentials, certificates, tenant
exports, employee payloads, and generated results do not belong in Git —
`.gitignore` and `scripts/check_repository.py` are a basic guardrail, not a
complete secret or personal-data scanner. See [SECURITY.md](SECURITY.md) for
deployment guidance and how to report a vulnerability.

## License

[Apache License 2.0](LICENSE). Copyright 2026 Justin Gong. See
[NOTICE](NOTICE) for third-party and migrated-code attribution.
