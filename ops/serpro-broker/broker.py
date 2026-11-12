#!/usr/bin/env python3
"""Run the isolated stdlib mTLS broker; never load the Django application."""

import os
from http.server import HTTPServer

from serpro_broker.cache import BrokerConfig, BrokerUnavailableError, TokenBroker
from serpro_broker.server import make_handler

__all__ = ["BrokerConfig", "BrokerUnavailableError", "TokenBroker", "make_handler"]


def main() -> None:
    """Run with only the deployment secrets mounted into this service."""
    config = BrokerConfig(
        environment=os.environ.get("SERPRO_ENVIRONMENT", "fake"),
        secret=os.environ.get("SERPRO_BROKER_SECRET", ""),
        consumer_key=os.environ.get("SERPRO_CONSUMER_KEY", ""),
        consumer_secret=os.environ.get("SERPRO_CONSUMER_SECRET", ""),
    )
    with HTTPServer(("0.0.0.0", 8080), make_handler(TokenBroker(config))) as server:  # noqa: S104 - private compose network, no published port
        server.serve_forever()


if __name__ == "__main__":
    main()
