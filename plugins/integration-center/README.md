# successfactors-toolkit-integration-center

A plugin for the successfactors-toolkit MCP server that reads SAP
SuccessFactors Integration Center definitions. It is read-only: it never
creates, changes, runs or deletes an integration.

## Install

Install it into the same environment (or image) as the server, which loads it
through the `successfactors_toolkit.plugins` entry point at startup:

```bash
pip install ./plugins/integration-center
```

The public Docker image does not include it.

## Tools

TODO: document the tools once they exist.
