"""Classification hooks shared by every integration test."""

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark tests by directory so new integration files cannot be silently misclassified."""
    marker = pytest.mark.integration
    for item in items:
        item.add_marker(marker)
