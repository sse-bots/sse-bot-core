# SSE Bot Core

Shared Python modules used by SSE Discord bots.

## Install

```bash
pip install --upgrade --no-cache-dir git+https://github.com/sse-bots/sse-bot-core.git@main
```

## Imports

```python
from sse_bot_core.activation import setup_activation, _configure_srv_dns
from sse_bot_core.help_menu import setup_help
from sse_bot_core.heartbeat_client import start_heartbeat
from sse_bot_core.keep_alive import keep_alive
```

Keep bot-specific commands, database models, and business logic in each bot repository.
The `main` branch is the live update channel: deployments install the latest commit.
Test core changes in a staging bot before merging them into `main`.
