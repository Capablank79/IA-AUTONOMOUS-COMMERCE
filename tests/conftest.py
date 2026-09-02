from pathlib import Path


def pytest_configure() -> None:
    Path(".runtime").mkdir(exist_ok=True)
