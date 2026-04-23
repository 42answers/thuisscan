"""
DSO Presenteren v8 — full-annotaties dump + Haiku-vergelijking.

Doel: definitief uitsluiten of bevestigen dat DSO Presenteren v8 ergens
gestructureerde max_bouwhoogte / max_goothoogte data bevat — buiten de tot
nu toe geteste `omgevingsnormen`-array (die altijd leeg blijkt).

Aanpak:
  1. Voor 5 representatieve adressen (mix van plan-types) halen we
     `regeltekstannotaties/_zoek` op met `_expand=true`.
  2. We dumpen de volledige JSON-response naar /tmp/dso_dumps/{slug}.json.
  3. We scannen de response op ELK numeriek attribuut waarvan de naam of
     parent-pad wijst op hoogte (bouwhoogte, goothoogte, hoogte, maatvoering).
  4. Parallel draaien we Haiku op de geconcateneerde regelteksten en noteren
     `max_bouwhoogte_m` / `max_goothoogte_m`.
  5. Eindrapport per adres: structured-paden gevonden? matchen ze met Haiku?

Run vanaf project root:
  fly ssh console -C "cd /app/apps/api && python3 tests/dso_structured_hoogte_probe.py"

Of lokaal (vereist DSO_API_KEY + ANTHROPIC_API_KEY in env):
  cd apps/api && python3 tests/dso_structured_hoogte_probe.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

# Adapters-pad
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import httpx

from adapters import dso, bp_extractor

DUMP_DIR = "/tmp/dso_dumps"
os.makedirs(DUMP_DIR, exist_ok=True)

# 5 adressen — bewust gemixt op plan-leeftijd/type:
# - klassiek BP (kans op gestructureerde maatvoering hoog)
# - bruidsschat-omgevingsplan (vermoedelijk leeg)
# - moderne post-2024 omgevingsplan (onbekend)
ADRESSEN = [
    {
        "slug": "amsterdam-paramaribo7",
        "label": "Amsterdam — Paramaribostraat 7 (binnen ring, gestapeld)",
        "rd": (118918, 487447),
        "type_verwacht": "Omgevingsplan Amsterdam (bruidsschat)",
    },
    {
        "slug": "rotterdam-witte-de-with",
        "label": "Rotterdam — Witte de Withstraat 50 (binnenstad)",
        "rd": (92500, 437200),
        "type_verwacht": "Omgevingsplan Rotterdam",
    },
    {
        "slug": "utrecht-leidsche-rijn",
        "label": "Utrecht — Leidsche Rijn nieuwbouw (post-2010 BP)",
        "rd": (130850, 456400),
        "type_verwacht": "Omgevingsplan Utrecht",
    },
    {
        "slug": "deventer-binnenstad",
        "label": "Deventer — binnenstad beschermd gezicht",
        "rd": (208300, 472900),
        "type_verwacht": "Omgevingsplan Deventer",
    },
    {
        "slug": "wageningen-markt",
        "label": "Wageningen — Markt centrum",
        "rd": (173800, 443900),
        "type_verwacht": "Omgevingsplan Wageningen",
    },
]


# Trefwoorden in attribuut-PADEN waar hoogte-data zou kunnen leven.
HOOGTE_PAD_TREFWOORDEN = (
    "hoogte", "bouwhoogte", "goothoogte", "nokhoogte",
    "maatvoering", "norm", "kwantitatieveWaarde",
    "waarde", "eenheid", "omgevingsnorm", "gebiedsaanwijzing",
)


def _walk(obj: Any, pad: str = "") -> list[tuple[str, Any]]:
    """Yield (pad, waarde) voor elk leaf-veld in een geneste dict/list."""
    out: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            sub = f"{pad}.{k}" if pad else k
            if isinstance(v, (dict, list)):
                out.extend(_walk(v, sub))
            else:
                out.append((sub, v))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            sub = f"{pad}[{i}]"
            if isinstance(v, (dict, list)):
                out.extend(_walk(v, sub))
            else:
                out.append((sub, v))
    else:
        out.append((pad, obj))
    return out


def _vind_hoogte_attributen(blob: dict) -> list[dict]:
    """Loop door alle leaf-velden, selecteer die naar hoogte ruiken."""
    treffers: list[dict] = []
    for pad, waarde in _walk(blob):
        pad_lower = pad.lower()
        # Pad bevat een trefwoord?
        if not any(kw.lower() in pad_lower for kw in HOOGTE_PAD_TREFWOORDEN):
            continue
        # Voor numerieke leafs: noteer altijd. Voor strings: alleen als de
        # waarde een getal of "X meter"-patroon bevat.
        if isinstance(waarde, (int, float)):
            treffers.append({"pad": pad, "waarde": waarde, "type": "num"})
        elif isinstance(waarde, str) and waarde:
            wl = waarde.lower()
            if any(c.isdigit() for c in wl) and (
                "meter" in wl or "m " in wl or wl.endswith("m") or "hoogte" in wl
            ):
                treffers.append({"pad": pad, "waarde": waarde[:120], "type": "str"})
            elif waarde in ("meter", "m"):  # eenheid-sleutel
                treffers.append({"pad": pad, "waarde": waarde, "type": "eenheid"})
        elif isinstance(waarde, bool):
            pass  # niet interessant
    return treffers


async def _fetch_full_annotaties(
    client: httpx.AsyncClient, regeling_uri: str, rd_x: float, rd_y: float
) -> dict | None:
    """Identiek aan dso._fetch_regeling_annotaties maar bewaart de hele blob."""
    enc = dso._encode_uri(regeling_uri)
    url = f"{dso.DSO_PRES_BASE}/regelingen/{enc}/regeltekstannotaties/_zoek"
    body = {"geometrie": {"type": "Point", "coordinates": [rd_x, rd_y]}}
    auth = dso._auth_headers() or {}
    headers = {**auth, "Content-Type": "application/json", "Content-Crs": dso.RD_CRS}
    try:
        resp = await client.post(
            url,
            params={"_expand": "true", "size": 50},
            headers=headers,
            json=body,
            timeout=dso.TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        return {"_error": f"HTTP {e.response.status_code}", "_body": e.response.text[:500]}
    except Exception as e:
        return {"_error": repr(e)}


async def _probe_adres(adres: dict) -> dict:
    """Hoofd-werker per adres."""
    rd_x, rd_y = adres["rd"]
    print(f"\n=== {adres['label']} ===")
    print(f"    RD: ({rd_x}, {rd_y})")
    headers = dso._auth_headers()
    if not headers:
        return {**adres, "fout": "DSO_API_KEY niet gezet"}

    async with httpx.AsyncClient(timeout=dso.TIMEOUT_S, headers=headers) as client:
        # Stap 1: omgevingsplan vinden
        regs = await dso._zoek_regelingen(client, rd_x, rd_y)
        if not regs:
            return {**adres, "fout": "geen regelingen gevonden"}
        op = dso._pick_omgevingsplan(regs)
        if op is None:
            return {**adres, "fout": "geen omgevingsplan in regelingen"}
        print(f"    Plan: {op.officiele_titel}")
        print(f"    URI:  {op.uri_identificatie}")

        # Stap 2: full annotaties dump
        blob = await _fetch_full_annotaties(client, op.uri_identificatie, rd_x, rd_y)
        if blob is None:
            return {**adres, "fout": "annotaties-call faalde", "plan": op.officiele_titel}
        if "_error" in blob:
            return {**adres, "fout": blob["_error"], "plan": op.officiele_titel}

        # Dump naar bestand
        dump_path = os.path.join(DUMP_DIR, f"{adres['slug']}.json")
        with open(dump_path, "w") as f:
            json.dump(blob, f, indent=2, ensure_ascii=False)
        print(f"    Dump: {dump_path}  ({len(json.dumps(blob))} bytes)")

        # Stap 3: top-level structuur tellen
        top_keys = list(blob.keys())
        omg_norms = blob.get("omgevingsnormen") or []
        gebieds = blob.get("gebiedsaanwijzingen") or []
        loc_aand = blob.get("locatieaanduidingen") or []
        regelteksten = blob.get("regelteksten") or []
        print(f"    Top-level keys: {top_keys}")
        print(f"    omgevingsnormen={len(omg_norms)}  gebiedsaanwijzingen={len(gebieds)}  "
              f"locatieaanduidingen={len(loc_aand)}  regelteksten={len(regelteksten)}")

        # Stap 4: zoek hoogte-attributen door de hele blob
        treffers = _vind_hoogte_attributen(blob)
        if treffers:
            print(f"    GEVONDEN: {len(treffers)} hoogte-attributen:")
            for t in treffers[:15]:
                print(f"      [{t['type']}] {t['pad']} = {t['waarde']}")
            if len(treffers) > 15:
                print(f"      ... +{len(treffers) - 15} meer (zie dump)")
        else:
            print(f"    GEEN hoogte-attributen in annotaties-blob.")

        # Stap 5: subtype-inventarisatie van gebiedsaanwijzingen.
        # `type` is een dict: {"code": "...", "waarde": "natuur"}.
        if gebieds:
            subtypes: dict[str, int] = {}
            for g in gebieds:
                t_obj = g.get("type") or g.get("typeGebiedsaanwijzing") or {}
                t = t_obj.get("waarde") if isinstance(t_obj, dict) else str(t_obj)
                t = t or "onbekend"
                subtypes[t] = subtypes.get(t, 0) + 1
            print(f"    Gebiedsaanwijzing-subtypes: {subtypes}")
            # Toon de eerste gebiedsaanwijzing volledig — soms zit hoogte
            # impliciet in de naam (bv. "max bouwhoogte 11 meter")
            print(f"    Eerste gebiedsaanwijzing-naam: {gebieds[0].get('naam')!r}")

        # Stap 6: Haiku-extractie als referentie-waarheid
        haiku = await _haiku_voor_locatie(client, op.uri_identificatie, rd_x, rd_y)
        if haiku:
            print(f"    Haiku: bouwhoogte={haiku.max_bouwhoogte_m} m, "
                  f"goothoogte={haiku.max_goothoogte_m} m, "
                  f"bouwlagen={haiku.max_bouwlagen}")
        else:
            print(f"    Haiku: geen extractie mogelijk")

        return {
            **adres,
            "plan": op.officiele_titel,
            "uri": op.uri_identificatie,
            "top_keys": top_keys,
            "n_omgevingsnormen": len(omg_norms),
            "n_gebiedsaanwijzingen": len(gebieds),
            "n_locatieaanduidingen": len(loc_aand),
            "n_regelteksten": len(regelteksten),
            "treffers": treffers,
            "haiku": {
                "max_bouwhoogte_m": haiku.max_bouwhoogte_m if haiku else None,
                "max_goothoogte_m": haiku.max_goothoogte_m if haiku else None,
                "max_bouwlagen": haiku.max_bouwlagen if haiku else None,
            } if haiku else None,
        }


async def _haiku_voor_locatie(
    client: httpx.AsyncClient, regeling_uri: str, rd_x: float, rd_y: float
):
    """Pak regelteksten op locatie, draai Haiku op top-5 hoogte-relevante stukken."""
    enc = dso._encode_uri(regeling_uri)
    url = f"{dso.DSO_PRES_BASE}/regelingen/{enc}/regeltekstannotaties/_zoek"
    body = {"geometrie": {"type": "Point", "coordinates": [rd_x, rd_y]}}
    auth = dso._auth_headers() or {}
    hdr = {**auth, "Content-Type": "application/json", "Content-Crs": dso.RD_CRS}
    try:
        resp = await client.post(url, json=body, headers=hdr, params={"size": 50})
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    regelteksten = data.get("regelteksten") or []
    # FIX: filter `__art_` was te strikt voor Amsterdam-bruidsschat (alle wIds
    # zijn `gm0363_xxx__para_1` zonder __art_). Accepteer ook __para_, __sec_,
    # __subsec_ — dat zijn de daadwerkelijk inhoud-dragende componenten.
    INHOUD_PATRONEN = ("__art_", "__para_", "__subsec_", "__sec_")
    wIds = [
        r.get("wId") for r in regelteksten
        if r.get("wId") and any(p in r["wId"] for p in INHOUD_PATRONEN)
    ]
    print(f"      wIds-na-filter: {len(wIds)} (van {len(regelteksten)})")
    if not wIds:
        return None
    tekst = await dso.fetch_bouwhoogte_regeltekst(regeling_uri, wIds)
    if not tekst:
        return None
    try:
        return bp_extractor.extract_bp_regels(tekst)
    except Exception as e:
        print(f"      Haiku-fout: {e}")
        return None


async def main():
    if not os.getenv("DSO_API_KEY"):
        print("ERROR: DSO_API_KEY niet gezet — kan niet probe-en.")
        sys.exit(1)
    print(f"DSO Presenteren v8 hoogte-probe — {len(ADRESSEN)} adressen")
    print(f"Dumps -> {DUMP_DIR}/")

    rapport = []
    for adres in ADRESSEN:
        try:
            r = await _probe_adres(adres)
            rapport.append(r)
        except Exception as e:
            print(f"    FOUT: {e}")
            rapport.append({**adres, "fout": repr(e)})

    # Eindrapport
    print(f"\n\n{'='*70}\nEINDRAPPORT\n{'='*70}")
    print(f"{'Adres':<45} {'omgN':>5} {'gebA':>5} {'locA':>5} {'tref':>5} {'Haiku-h':>10}")
    print("-" * 80)
    for r in rapport:
        if r.get("fout"):
            print(f"{r['label'][:43]:<45}  FOUT: {r['fout'][:25]}")
            continue
        haiku_h = "-"
        if r.get("haiku") and r["haiku"]["max_bouwhoogte_m"] is not None:
            haiku_h = f"{r['haiku']['max_bouwhoogte_m']} m"
        print(f"{r['label'][:43]:<45} "
              f"{r.get('n_omgevingsnormen', 0):>5} "
              f"{r.get('n_gebiedsaanwijzingen', 0):>5} "
              f"{r.get('n_locatieaanduidingen', 0):>5} "
              f"{len(r.get('treffers', [])):>5} "
              f"{haiku_h:>10}")

    # Conclusie
    print(f"\n{'='*70}\nCONCLUSIE")
    print('='*70)
    aantal_met_treffers = sum(1 for r in rapport if r.get("treffers"))
    aantal_met_haiku = sum(1 for r in rapport if r.get("haiku") and r["haiku"]["max_bouwhoogte_m"] is not None)
    print(f"Adressen met structured hoogte-attributen: {aantal_met_treffers}/{len(rapport)}")
    print(f"Adressen met Haiku max_bouwhoogte:         {aantal_met_haiku}/{len(rapport)}")
    if aantal_met_treffers == 0 and aantal_met_haiku > 0:
        print("\n>>> DEFINITIEF: DSO Presenteren bevat geen structured hoogte-data.")
        print(">>> Haiku-extractie blijft de enige route naast Ruimtelijke Plannen v4.")
    elif aantal_met_treffers > 0:
        print("\n>>> Structured hoogte-data WEL aanwezig — zie /tmp/dso_dumps/*.json")
        print(">>> Volgende stap: bepaal exact attribuut-pad en vergelijk met Haiku-waarden.")


if __name__ == "__main__":
    asyncio.run(main())
