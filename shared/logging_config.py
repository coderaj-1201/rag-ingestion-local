"""
Logging — LOCAL DEV version.
Plain human-readable stdout logs.
"""
from __future__ import annotations

import logging
import sys


def configure_logging(service_name: str = "rag") -> None:
    from shared.config import settings

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    fmt = f"%(asctime)s  [{service_name}]  %(levelname)-8s  %(name)s — %(message)s"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    for noisy in ("azure.core", "azure.identity", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if settings.APPLICATIONINSIGHTS_CONNECTION_STRING:
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
            configure_azure_monitor(
                connection_string=settings.APPLICATIONINSIGHTS_CONNECTION_STRING,
                service_name=service_name,
            )
        except ImportError:
            pass


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
