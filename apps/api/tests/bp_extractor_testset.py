"""
Testset voor bp_extractor — 10 representatieve plan-tekst-samples.

Elk sample heeft:
- `naam`: korte beschrijving (gemeente + type)
- `tekst`: de ruwe NL-plantekst (gekopieerd van ruimtelijkeplannen.nl)
- `ground_truth`: handmatig bepaalde verwachte extractie-uitkomst

Run met:
    cd apps/api
    python3 tests/bp_extractor_testset.py

Rapporteert per-veld accuracy + mismatches. Target: 90%+ op max_bouwhoogte_m
(kritiek voor Optopping-card). Lager accepteren we voor nuance-velden zoals
`kap_verplicht` die vaak impliciet of onduidelijk zijn.

Samples gekozen voor diversiteit:
- Grote stad centrum (Amsterdam, Rotterdam)
- Naoorlogse rijtjes (Purmerend, Nieuwegein)
- Historisch centrum beschermd gezicht (Grave, Deventer)
- Vrijstaande landelijke woning (Drenthe, Zeeland)
- Appartementencomplex (Utrecht, Eindhoven)
- Modern omgevingsplan-stijl (post-2024)
"""
from __future__ import annotations

import os
import sys
from dataclasses import asdict

# Adapters-pad
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from adapters.bp_extractor import BPRegels, extract_bp_regels

SAMPLES = [
    {
        "naam": "1. Amsterdam centrum — Wonen, standaard",
        "tekst": """Ter plaatse van de aanduiding 'Wonen-1' zijn gronden bestemd voor wonen.
De bouwhoogte van hoofdgebouwen bedraagt maximaal 11 meter, gemeten vanaf peil.
De goothoogte bedraagt maximaal 9 meter. Bouwen met kap is verplicht.
Het bebouwingspercentage van het bouwvlak bedraagt ten hoogste 100%.""",
        "ground_truth": {
            "max_bouwhoogte_m": 11,
            "max_goothoogte_m": 9,
            "max_bouwlagen": None,
            "max_bebouwingspercentage": 100,
            "kap_verplicht": True,
            "plat_dak_toegestaan": False,
            "bestemming_contains": "Wonen",
        },
    },
    {
        "naam": "2. Rotterdam Centrum — hoogbouw, plat dak",
        "tekst": """Binnen de bestemming 'Centrum-2' mag de bouwhoogte niet meer bedragen
dan 18 meter. Er geldt geen kapverplichting; zowel een plat dak als een kap
zijn toegestaan. Het aantal bouwlagen bedraagt maximaal zes.""",
        "ground_truth": {
            "max_bouwhoogte_m": 18,
            "max_goothoogte_m": None,
            "max_bouwlagen": 6,
            "max_bebouwingspercentage": None,
            "kap_verplicht": False,
            "plat_dak_toegestaan": True,
            "bestemming_contains": "Centrum",
        },
    },
    {
        "naam": "3. Grave — beschermd gezicht, restrictief",
        "tekst": """Deze gronden hebben de dubbelbestemming 'Waarde-Cultuurhistorie'. Voor
hoofdgebouwen geldt een maximale goothoogte van 6 meter en een maximale
bouwhoogte van 10 meter. Kap verplicht, met een dakhelling tussen 45 en 60
graden. Bijgebouwen maximaal 3 meter goothoogte.""",
        "ground_truth": {
            "max_bouwhoogte_m": 10,
            "max_goothoogte_m": 6,
            "max_bouwlagen": None,
            "max_bebouwingspercentage": None,
            "kap_verplicht": True,
            "plat_dak_toegestaan": False,
            "bestemming_contains": "Waarde",
        },
    },
    {
        "naam": "4. Purmerend rijtjes — 70's woonwijk",
        "tekst": """De voor 'Wonen-2' aangewezen gronden zijn bestemd voor woningen. De
goothoogte bedraagt ten hoogste 6 meter, de bouwhoogte ten hoogste 10 meter.
Er wordt gebouwd in twee bouwlagen met kap. Bebouwingspercentage maximaal 60%.""",
        "ground_truth": {
            "max_bouwhoogte_m": 10,
            "max_goothoogte_m": 6,
            "max_bouwlagen": 2,
            "max_bebouwingspercentage": 60,
            "kap_verplicht": True,
            "plat_dak_toegestaan": False,
            "bestemming_contains": "Wonen",
        },
    },
    {
        "naam": "5. Nieuwegein nieuwbouw — plat dak toegestaan",
        "tekst": """Binnen de bestemming Wonen-Vrijstaand is bebouwing toegestaan tot een
bouwhoogte van 10 meter. Zowel plat als hellend dak is toegestaan. Het
bebouwingspercentage mag maximaal 40% van het perceel bedragen.""",
        "ground_truth": {
            "max_bouwhoogte_m": 10,
            "max_goothoogte_m": None,
            "max_bouwlagen": None,
            "max_bebouwingspercentage": 40,
            "kap_verplicht": False,
            "plat_dak_toegestaan": True,
            "bestemming_contains": "Wonen",
        },
    },
    {
        "naam": "6. Deventer historisch — gedetailleerd",
        "tekst": """Binnen de bestemming 'Gemengd - 1' is wonen op de verdiepingen toegestaan.
De bouwhoogte bedraagt maximaal 13 meter, gemeten vanaf peil, waarbij
ondergeschikte bouwdelen zoals schoorstenen, antenne-installaties en
lift-opbouwen tot maximaal 2 meter buiten deze hoogte mogen uitsteken. De
goothoogte is maximaal 9 meter. Dakhelling tussen 30 en 60 graden verplicht.""",
        "ground_truth": {
            "max_bouwhoogte_m": 13,
            "max_goothoogte_m": 9,
            "max_bouwlagen": None,
            "max_bebouwingspercentage": None,
            "kap_verplicht": True,
            "plat_dak_toegestaan": False,
            "bestemming_contains": "Gemengd",
        },
    },
    {
        "naam": "7. Drenthe landelijk — vrijstaande woning",
        "tekst": """Op gronden met de bestemming Wonen zijn vrijstaande woningen toegestaan.
Maximale inhoud woning 750 m³. Goothoogte maximaal 4 meter, nokhoogte
maximaal 9 meter. Bijgebouwen tot 80 m² zijn toegestaan.""",
        "ground_truth": {
            "max_bouwhoogte_m": 9,    # nokhoogte == bouwhoogte
            "max_goothoogte_m": 4,
            "max_bouwlagen": None,
            "max_bebouwingspercentage": None,
            "kap_verplicht": None,    # niet expliciet, gootafstand wel
            "plat_dak_toegestaan": None,
            "bestemming_contains": "Wonen",
        },
    },
    {
        "naam": "8. Utrecht appartementencomplex",
        "tekst": """De bestemming Wonen-4 betreft gestapelde woningbouw. Binnen het bouwvlak
mag de bouwhoogte maximaal 20 meter bedragen. Het aantal bouwlagen is
maximaal vier plus een kap. Er is geen kapverplichting, maar bij plat dak
geldt een afwijking van maximaal 2 meter voor installaties.""",
        "ground_truth": {
            "max_bouwhoogte_m": 20,
            "max_goothoogte_m": None,
            "max_bouwlagen": 4,   # "vier plus kap" - hoofdgetal is vier
            "max_bebouwingspercentage": None,
            "kap_verplicht": False,
            "plat_dak_toegestaan": True,
            "bestemming_contains": "Wonen",
        },
    },
    {
        "naam": "9. Eindhoven modern — omgevingsplan stijl",
        "tekst": """Op deze locatie is de activiteit 'bouwen van een woongebouw' toegestaan
onder de volgende voorwaarden: maximale hoogte 15 meter gemeten vanaf maaiveld;
minimaal 1 parkeerplaats per wooneenheid; bebouwing binnen het aangegeven
bouwvlak. Dakvorm vrij.""",
        "ground_truth": {
            "max_bouwhoogte_m": 15,
            "max_goothoogte_m": None,
            "max_bouwlagen": None,
            "max_bebouwingspercentage": None,
            "kap_verplicht": False,
            "plat_dak_toegestaan": True,
            "bestemming_contains": None,  # geen expliciet bestemmingstype in NL-bpl zin
        },
    },
    {
        "naam": "10. Zeeland rural — minimale regeltekst",
        "tekst": """Bestemming: Wonen. Maximale bouwhoogte: 11 m. Maximale goothoogte: 6 m.""",
        "ground_truth": {
            "max_bouwhoogte_m": 11,
            "max_goothoogte_m": 6,
            "max_bouwlagen": None,
            "max_bebouwingspercentage": None,
            "kap_verplicht": None,
            "plat_dak_toegestaan": None,
            "bestemming_contains": "Wonen",
        },
    },
]


def _field_correct(actual, expected, tol=0.5):
    """Vergelijk extracted vs ground-truth voor één veld."""
    if expected is None:
        return actual is None
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(actual - expected) <= tol
    return actual == expected


def _bestemming_correct(actual: str, expected_contains):
    """Losse check: bestemming bevat het verwachte substring (case-insensitive)."""
    if expected_contains is None:
        return True  # niet gespecificeerd, niet fout
    if not actual:
        return False
    return expected_contains.lower() in actual.lower()


def run_validation():
    """Draai de testset, rapporteer per-veld accuracy."""
    import time

    totals: dict[str, list[bool]] = {
        "max_bouwhoogte_m": [],
        "max_goothoogte_m": [],
        "max_bouwlagen": [],
        "max_bebouwingspercentage": [],
        "kap_verplicht": [],
        "plat_dak_toegestaan": [],
        "bestemming": [],
    }
    mismatches = []
    print(f"Testset: {len(SAMPLES)} samples\n")

    for i, sample in enumerate(SAMPLES, 1):
        t0 = time.time()
        result = extract_bp_regels(sample["tekst"])
        dt = time.time() - t0
        gt = sample["ground_truth"]
        if result is None:
            print(f"{sample['naam']}: EXTRACTIE FAALDE  ({dt:.1f}s)")
            for k in totals:
                totals[k].append(False)
            continue
        per_field_ok = {}
        for k in ("max_bouwhoogte_m", "max_goothoogte_m", "max_bouwlagen",
                  "max_bebouwingspercentage", "kap_verplicht", "plat_dak_toegestaan"):
            ok = _field_correct(getattr(result, k), gt.get(k))
            totals[k].append(ok)
            per_field_ok[k] = ok
        ok_bes = _bestemming_correct(result.bestemming, gt.get("bestemming_contains"))
        totals["bestemming"].append(ok_bes)
        per_field_ok["bestemming"] = ok_bes

        all_ok = all(per_field_ok.values())
        status = "✓" if all_ok else "✗"
        print(f"{status} {sample['naam']}  ({dt:.1f}s)")
        for k, ok in per_field_ok.items():
            if not ok:
                print(f"   MISS {k}: got={getattr(result, k, None)}  expected~={gt.get(k if k != 'bestemming' else 'bestemming_contains')}")
        if not all_ok:
            mismatches.append((sample["naam"], asdict(result), gt))

    print(f"\n{'='*60}\nAccuracy per veld:\n{'='*60}")
    for k, results in totals.items():
        pct = 100 * sum(results) / max(1, len(results))
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        print(f"  {k:<32}  {pct:>5.1f}%  {bar}")

    overall = sum(sum(r) for r in totals.values()) / sum(len(r) for r in totals.values())
    print(f"\n  OVERALL{' ' * 26}  {100*overall:>5.1f}%")
    print(f"\nSamples met minstens één mismatch: {len(mismatches)}/{len(SAMPLES)}")


if __name__ == "__main__":
    run_validation()
