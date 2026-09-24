"""ABG Intelligence Terminal v3 - multi-source stock analysis engine for AI Business Group.

Quick start (library)::

    import asyncio
    from abg import AnalysisEngine

    async def main():
        async with AnalysisEngine() as eng:
            report = await eng.analyze("AAPL", period="1y")
            print(report["signal"], report["risk"][0]["level"])

    asyncio.run(main())
"""
__version__ = "3.0.0"

from .config import Settings, get_settings  # noqa: E402
from .engine import AnalysisEngine, AnalyzeOptions  # noqa: E402

__all__ = ["AnalysisEngine", "AnalyzeOptions", "Settings", "get_settings", "__version__"]
