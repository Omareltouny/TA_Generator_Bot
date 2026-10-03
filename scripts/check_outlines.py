"""Live acceptance check: run real outlines through the REAL LLM router + parser + planner.

Usage (keys in env / .env exported):  python scripts/check_outlines.py [outline.pdf ...]
Defaults to every PDF in tests/fixtures. Writes parsed JSON to ./check_out/ so you can inspect it.
"""
import asyncio
import json
import pathlib
import sys

from bot.config import Config
from bot.services.llm_router import AllProvidersBusy, build_router
from bot.services.outline_parser import parse_outline
from bot.services.pdf_text import extract_pdf
from bot.services.planner import build_plan

# (labs+tutorials count, assignments count, expect all inferred?) taken from reading each outline by hand
EXPECT = {
    "math1920": (12, 3, False), "cs4650": (10, 3, False), "cs4350": (9, 3, False),
    "cs2920": (6, 3, False), "mcs3320": (11, 3, True), "cs3130": (None, 3, False), "cs2820": (None, 3, None),
}


async def main(paths):
    cfg = Config.from_env()
    llm = build_router(cfg)
    if not llm.providers:
        sys.exit("No provider key set (GEMINI_API_KEY / GROQ_API_KEY / OPENROUTER_API_KEY).")
    out_dir = pathlib.Path("check_out")
    out_dir.mkdir(exist_ok=True)
    bad = 0
    for p in paths:
        key = p.stem.lower()
        text = extract_pdf(p.read_bytes()).text
        for attempt in range(4):
            try:
                outline, res = await parse_outline(llm, text)
                break
            except AllProvidersBusy as e:
                print(f"  rate-limited, waiting {int(e.retry_after)}s ...")
                await asyncio.sleep(min(e.retry_after, 90) + 1)
        else:
            print(f"{p.name}: FAILED (providers stayed busy)")
            bad += 1
            continue
        (out_dir / f"{p.stem}.json").write_text(outline.model_dump_json(indent=2))
        plan = build_plan(outline)
        weekly = [d for d in plan if d.type != "assignment"]
        ass = [d for d in plan if d.type == "assignment"]
        print(f"\n== {p.name}  [{res.provider}/{res.model}]")
        print(f"   {outline.course_code} | {outline.course_name} | {outline.term} | stack={outline.language_hint} | software={outline.software}")
        print(f"   schedule rows={len(outline.schedule)} assessments={[a.name for a in outline.assessments]}")
        print(f"   plan: {len(weekly)} weekly items (weeks {[d.week for d in weekly]}), assignments={[(d.title, d.week) for d in ass]}")
        exp = next((v for k, v in EXPECT.items() if k in key or k.replace('-', '') in key.replace('-', '').replace('_', '')), None)
        if exp:
            checks = []
            if exp[0] is not None:
                checks.append(("weekly items", len(weekly), exp[0]))
            checks.append(("assignments", len(ass), exp[1]))
            if exp[2] is not None:
                checks.append(("all inferred", all(d.source == "inferred" for d in weekly), exp[2]))
            for name, got, want in checks:
                ok = got == want
                bad += not ok
                print(f"   {'PASS' if ok else 'FAIL'}  {name}: got {got}, expected {want}")
        await asyncio.sleep(4)  # be gentle with free-tier rate limits
    print("\nAll checks passed." if not bad else f"\n{bad} check(s) failed - inspect check_out/*.json and the printed plan.")


if __name__ == "__main__":
    files = [pathlib.Path(a) for a in sys.argv[1:]] or sorted(pathlib.Path("tests/fixtures").glob("*.pdf"))
    asyncio.run(main(files))
