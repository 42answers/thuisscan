"""
Forensisch onderzoek: zoek de bouwhoogte voor Paramaribostraat 7.

Drie checks naast elkaar:
  A) Print de top-5 regelteksten die Haiku nu krijgt — zien we een getal?
  B) Scan ALLE regelteksten (niet alleen top 250) op concreet hoogte-getal
     via regex `\b\d{1,2}([,.]\d+)?\s*(m|meter)\b`
  C) Zoek specifiek op woorden die naar de verbeelding verwijzen ("zoals
     aangegeven", "bouwvlak", "aanduiding") — dan weten we of de data in
     tekst staat of op de kaart.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import httpx

from adapters import dso

ADRES = {"label": "Paramaribostraat 7 Amsterdam", "rd": (118918, 487447)}

HOOGTE_NUMRE = re.compile(
    r"\b(?:bouwhoogte|goothoogte|nokhoogte|hoogte)\s+(?:van\s+|bedraagt\s+|niet\s+meer\s+dan\s+|maximaal\s+|max\.\s*)?"
    r"(\d{1,2}(?:[,.]\d+)?)\s*(?:m|meter)\b",
    re.IGNORECASE,
)
ANY_METER_RE = re.compile(r"\b(\d{1,2}(?:[,.]\d+)?)\s*(?:m|meter)\b")
VERBEELDING_RE = re.compile(
    r"(?:zoals\s+aangegeven|op\s+de\s+verbeelding|ter\s+plaatse\s+van\s+de\s+aanduiding|"
    r"bouwvlak|maatvoerings[a-z]*|verbeelding)",
    re.IGNORECASE,
)


async def main():
    if not os.getenv("DSO_API_KEY"):
        sys.exit("DSO_API_KEY niet gezet")

    headers = dso._auth_headers()
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        regs = await dso._zoek_regelingen(client, *ADRES["rd"])
        op = dso._pick_omgevingsplan(regs)
        if op is None:
            sys.exit("geen omgevingsplan")
        print(f"Plan: {op.officiele_titel}")

        enc = dso._encode_uri(op.uri_identificatie)
        url = f"{dso.DSO_PRES_BASE}/regelingen/{enc}/regeltekstannotaties/_zoek"
        body = {"geometrie": {"type": "Point", "coordinates": list(ADRES["rd"])}}
        hdr = {**headers, "Content-Type": "application/json", "Content-Crs": dso.RD_CRS}
        resp = await client.post(url, json=body, headers=hdr, params={"size": 50})
        resp.raise_for_status()
        data = resp.json()
        regelteksten = data.get("regelteksten") or []
        print(f"Regelteksten op locatie: {len(regelteksten)}")

        _PAT = ("__art_", "__para_", "__subsec_", "__sec_")
        wIds = [r["wId"] for r in regelteksten if r.get("wId") and any(p in r["wId"] for p in _PAT)]
        print(f"wIds-na-filter: {len(wIds)}")

        # Parallel alles ophalen — 1461 × ~200ms / 20 sem = ~15s
        print(f"\nAlle {len(wIds)} teksten ophalen in parallel (sem=20)...")
        sem = asyncio.Semaphore(20)
        alle_teksten: list[tuple[str, str]] = []

        async def _w(wId):
            async with sem:
                t = await dso._fetch_regeltekst_tekst(client, op.uri_identificatie, wId)
                if t:
                    alle_teksten.append((wId, t))

        await asyncio.gather(*[_w(w) for w in wIds])
        print(f"Opgehaalde teksten: {len(alle_teksten)}")

    # ============ CHECK A: Top-5 gescoord (wat Haiku krijgt) ============
    print("\n" + "="*72)
    print("CHECK A — Top-5 regelteksten die Haiku nu ziet")
    print("="*72)
    scored = [(dso._relevantie_score(t), wId, t) for wId, t in alle_teksten]
    scored.sort(reverse=True, key=lambda x: x[0])
    for i, (score, wId, t) in enumerate(scored[:5], 1):
        print(f"\n--- #{i} score={score} wId={wId} ({len(t)} chars) ---")
        print(t[:1200])
        if len(t) > 1200:
            print(f"... (+{len(t)-1200} chars)")

    # ============ CHECK B: Concrete hoogte-getal in ALLE teksten ============
    print("\n" + "="*72)
    print("CHECK B — Directe hoogte-getal-hits in ALLE regelteksten")
    print("="*72)
    direct_hits = []
    any_meter_hits = []
    for wId, t in alle_teksten:
        m1 = HOOGTE_NUMRE.findall(t)
        if m1:
            direct_hits.append((wId, m1[:3], t))
        m2 = ANY_METER_RE.findall(t)
        if m2:
            any_meter_hits.append((wId, m2[:5], t))
    print(f"\nDirecte 'hoogte X meter'-patronen: {len(direct_hits)} regelteksten")
    for wId, matches, t in direct_hits[:8]:
        print(f"\n  wId={wId}")
        print(f"    matches: {matches}")
        idx = max(0, t.lower().find(matches[0].split()[-1] if matches else "m") - 100)
        snippet = t[idx:idx+300]
        print(f"    context: ...{snippet}...")

    print(f"\n'Elk getal met m/meter' (breder): {len(any_meter_hits)} regelteksten "
          f"(sommige zijn afstanden, m² e.d.)")
    # Toon er 5 als diagnostische sample
    for wId, matches, t in any_meter_hits[:5]:
        if HOOGTE_NUMRE.search(t):  # al gerapporteerd in direct_hits
            continue
        print(f"\n  wId={wId} matches={matches}")
        # Zoek een snippet rond 'hoogte' of 'bouw'
        lo = t.lower()
        for kw in ("hoogte", "bouw", "woon", "verdieping"):
            p = lo.find(kw)
            if p != -1:
                print(f"    near '{kw}': ...{t[max(0,p-50):p+200]}...")
                break

    # ============ CHECK C: Verbeelding-verwijzingen ============
    print("\n" + "="*72)
    print("CHECK C — Verwijzen regelteksten naar 'de verbeelding'?")
    print("="*72)
    verb_hits = [(wId, t) for wId, t in alle_teksten if VERBEELDING_RE.search(t)]
    print(f"\nRegelteksten met verbeelding/bouwvlak-verwijzing: {len(verb_hits)}")
    for wId, t in verb_hits[:3]:
        match = VERBEELDING_RE.search(t)
        p = match.start()
        print(f"\n  wId={wId}")
        print(f"    snippet: ...{t[max(0,p-80):p+250]}...")

    # ============ Conclusie ============
    print("\n" + "="*72)
    print("DIAGNOSE")
    print("="*72)
    if direct_hits:
        print(f"✅ In {len(direct_hits)} van {len(alle_teksten)} regelteksten staat een CONCREET hoogte-getal.")
        print(f"   Haiku mist die omdat hij alleen de top-5 scorers ziet — niet deze.")
        print(f"   Fix: filter specifiek op '\\d+ m(eter)?' voor hoogte-context, niet op keyword-count.")
    else:
        print(f"❌ In 0 van {len(alle_teksten)} regelteksten staat een concreet hoogte-getal.")
        print(f"   Data zit dan op de verbeelding (kaart/polygoon-maatvoering), niet in de tekst.")
        print(f"   Verbeelding-verwijzingen gevonden: {len(verb_hits)} — bevestigt dit.")
        print(f"   Route naar waarde: RP v4 maatvoeringsvlakken (geometrisch).")


if __name__ == "__main__":
    asyncio.run(main())
