"""Using the terminal as a Python library (notebooks, scripts, backtests).

Run offline:  python examples/library_usage.py --demo
"""
import asyncio
import sys

from abg import AnalysisEngine, AnalyzeOptions, Settings


async def main(demo: bool):
    settings = Settings(allow_synthetic=demo, **({"provider_order": "synthetic"} if demo else {}))
    async with AnalysisEngine(settings) as eng:
        # 1) one full report (dict, JSON-safe)
        r = await eng.analyze("AAPL", AnalyzeOptions(period="6mo", ai=False))
        print(r["symbol"], r["signal"]["label"], r["signal"]["score"], "| risk:", r["risk"][0]["level"])
        print("top setup:", r["plays"][0]["name"], r["plays"][0]["levels"])

        # 2) raw data with provenance (which vendor answered, latency, cache state)
        h = await eng.history("MSFT", "1y")
        print(len(h.value), "bars from", h.provenance.provider, f"in {h.provenance.latency_ms} ms")

        # 3) many symbols concurrently
        many = await eng.analyze_many(["NVDA", "AMD", "INTC"], concurrency=3, ai=False, options=False)
        for sym, rep in many.items():
            print(sym, rep.get("signal", {}).get("score"), rep.get("error", ""))

        # 4) training data for a future risk model (features + forward labels, schema-versioned)
        frame = await eng.feature_frame("AAPL", "2y", labels=True)
        print(frame.shape, "schema", frame.attrs["schema_version"])


if __name__ == "__main__":
    asyncio.run(main("--demo" in sys.argv))
