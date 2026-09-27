"""Bounded read-only monitoring adapters for Incident Operations."""

from app.adapters.monitoring.alertmanager import BoundedAlertmanagerReader

__all__ = ["BoundedAlertmanagerReader"]
