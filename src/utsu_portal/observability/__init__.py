"""Unified observability module for OpenTelemetry tracing, metrics, and logging.

This module provides a vendor-agnostic observability stack that works identically
across Kubernetes, Azure Container Apps, GCP Cloud Run, and AWS Fargate.

Key Features:
- Distributed tracing (trace context propagated across services)
- Metrics collection (CPU, memory, request latency, business metrics)
- Structured logging (JSON format with trace IDs)
- Automatic instrumentation of HTTP, database, and async operations
- Cloud-agnostic configuration via environment variables

Usage:
    from utsu_portal.observability import setup_observability, get_tracer, get_logger
    
    setup_observability(service_name="mailsvc")
    tracer = get_tracer(__name__)
    logger = get_logger(__name__)
    
    with tracer.start_as_current_span("send_email"):
        logger.info("Sending email", extra={"user_id": 123})
"""

import os
import json
import logging
from typing import Optional
from logging.config import dictConfig

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.jaeger import JaegerPropagator


class JSONFormatter(logging.Formatter):
    """Format logs as JSON for ELK/Datadog/Cloud Logging ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        """Convert log record to JSON."""
        log_data = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add trace context if available
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            log_data["trace_id"] = format(span_context.trace_id, "032x")
            log_data["span_id"] = format(span_context.span_id, "016x")
        
        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields (custom context data)
        if hasattr(record, "extra_fields"):
            log_data.update(record.extra_fields)
        
        return json.dumps(log_data)


class ContextFilter(logging.Filter):
    """Add extra fields to log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Add context fields if they exist."""
        # This gets populated by loggers using extra={"key": "value"}
        if not hasattr(record, "extra_fields"):
            record.extra_fields = {}
        return True


def setup_observability(
    service_name: str,
    service_version: str = "1.0.0",
    jaeger_enabled: bool = True,
    prometheus_enabled: bool = True,
) -> None:
    """
    Initialize unified observability stack.
    
    Configuration via environment variables:
    - JAEGER_AGENT_HOST: Jaeger agent host (default: localhost)
    - JAEGER_AGENT_PORT: Jaeger agent port (default: 6831)
    - OTEL_LOG_LEVEL: Logging level (default: INFO)
    
    Args:
        service_name: Name of the service (e.g., "authsvc", "mailsvc")
        service_version: Service version for resource tracking
        jaeger_enabled: Enable Jaeger distributed tracing
        prometheus_enabled: Enable Prometheus metrics export
    """
    
    # 1. Create resource identifying the service
    resource = Resource.create({
        "service.name": service_name,
        "service.version": service_version,
        "service.namespace": os.getenv("OTEL_SERVICE_NAMESPACE", "utsu-portal"),
    })
    
    # 2. Setup distributed tracing with Jaeger
    if jaeger_enabled:
        jaeger_exporter = JaegerExporter(
            agent_host_name=os.getenv("JAEGER_AGENT_HOST", "localhost"),
            agent_port=int(os.getenv("JAEGER_AGENT_PORT", "6831")),
        )
        trace_provider = TracerProvider(resource=resource)
        trace_provider.add_span_processor(BatchSpanProcessor(jaeger_exporter))
        trace.set_tracer_provider(trace_provider)
    
    # 3. Setup metrics with Prometheus
    if prometheus_enabled:
        prometheus_reader = PrometheusMetricReader()
        metrics_provider = MeterProvider(resource=resource, metric_readers=[prometheus_reader])
        metrics.set_meter_provider(metrics_provider)
    
    # 4. Setup W3C trace context propagation (cross-service tracing)
    set_global_textmap(JaegerPropagator())
    
    # 5. Auto-instrument common libraries
    LoggingInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    RequestsInstrumentor().instrument()
    AsyncioInstrumentor().instrument()
    SQLAlchemyInstrumentor().instrument()
    
    # 6. Setup structured JSON logging
    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": JSONFormatter,
            },
            "standard": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            },
        },
        "filters": {
            "context": {
                "()": ContextFilter,
            },
        },
        "handlers": {
            "default": {
                "level": os.getenv("OTEL_LOG_LEVEL", "INFO"),
                "class": "logging.StreamHandler",
                "formatter": "json",
                "filters": ["context"],
            },
        },
        "loggers": {
            "": {
                "handlers": ["default"],
                "level": os.getenv("OTEL_LOG_LEVEL", "INFO"),
                "propagate": True,
            }
        }
    }
    dictConfig(logging_config)


def get_tracer(module_name: str) -> trace.Tracer:
    """Get a tracer instance for a module."""
    return trace.get_tracer(module_name)


def get_logger(module_name: str) -> logging.Logger:
    """Get a logger instance for a module."""
    return logging.getLogger(module_name)


def instrument_fastapi(app) -> None:
    """Instrument a FastAPI application."""
    FastAPIInstrumentor.instrument_app(app)


def instrument_flask(app) -> None:
    """Instrument a Flask application."""
    FlaskInstrumentor().instrument_app(app)


__all__ = [
    "setup_observability",
    "get_tracer",
    "get_logger",
    "instrument_fastapi",
    "instrument_flask",
    "JSONFormatter",
    "ContextFilter",
]
