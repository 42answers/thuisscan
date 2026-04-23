"""
Verbouwing-adapter — concrete verbouwingsmogelijkheden per pand.

Fase 1 MVP (deze module) levert:

1. **Kadastraal perceel** via PDOK BRK-Publiek WFS (kadastralekaart:Perceel)
   → polygoon + oppervlakte in m² (RD).
2. **Beschermd stads-/dorpsgezicht** via RCE WFS (rce:Townscapes)
   → ja/nee + gezichtnaam (bv. "Amsterdam - Binnen de Singelgracht").
3. **Achtererf-analyse** lokaal via Shapely:
     perceel_polygoon − pand_polygoon = onbebouwd terrein
     centroid-heuristiek: de helft van onbebouwd tegenovergesteld aan het
     adres-entrypoint = achtererfgebied.

Fase 2 (later) voegt toe: bestemmingsplan-regels via DSO-API + Claude Haiku
extractor voor "max bouwhoogte", beslisboom-cards (uitbouw, optopping, dak-
kapel, tuinhuis), voor/achter-heuristiek verfijning.

Alle ruimtelijke berekeningen in RD (EPSG:28992) zodat oppervlakte in echte
meters uitkomt. BAG + BRK + RCE leveren allemaal direct in RD.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Optional

import httpx
from shapely.geometry import shape, Polygon, MultiPolygon, Point
from shapely.ops import unary_union

from . import gemeentelijk_monument, dso, vergunningcheck, bag3d, bp_extractor, bijgebouwen, wkpb, bag_vbos

# --- WFS endpoints ---
BRK_WFS = "https://service.pdok.nl/kadaster/kadastralekaart/wfs/v5_0"
RCE_WFS = "https://services.rce.geovoorziening.nl/rce/wfs"
BAG_WFS = "https://service.pdok.nl/lv/bag/wfs/v2_0"

TIMEOUT_S = 10.0
HEADERS = {"User-Agent": "buurtscan/1.0 (nl-NL) contact:vandeweijer@gmail.com"}

# Burenrecht: min. afstand tot erfgrens voor een gesloten bouwwerk = 0 m voor
# een vergunningvrije aanbouw (Bbl), maar praktisch houden we 1 m aan voor
# goot/dakoverstek + installatie-ruimte.
ERFGRENS_MARGE_M = 1.0


@dataclass
class Perceel:
    """Kadastraal perceel (BRK-Publiek)."""
    perceelnummer: int
    gemeente_code: str               # AKR-gemeentecode (niet CBS)
    oppervlakte_m2: int              # kadastraleGrootteWaarde
    # Polygoon-geometrie in RD. Niet geserialiseerd naar frontend; alleen
    # intern gebruikt voor Shapely-berekeningen.
    polygon_rd: Optional[Polygon] = None


@dataclass
class BeschermdGezicht:
    """Rijksbeschermd stads- of dorpsgezicht (RCE Townscapes)."""
    naam: str                        # bv "Amsterdam - Binnen de Singelgracht"
    status: str                      # bv "rijksbeschermd stads- of dorpsgezicht"
    aangewezen: Optional[str] = None # datum aanwijzing (indien bekend)


@dataclass
class AchtererfAnalyse:
    """Resultaat van perceel − pand = onbebouwd + voor/achter-detectie."""
    onbebouwd_m2: int                # totaal onbebouwd terrein op perceel
    onbebouwd_pct: float             # onbebouwd / perceel * 100
    achtererf_m2: int                # heuristisch: helft tegenovergesteld aan adres
    uitbouw_diepte_max_m: Optional[float] = None  # max diepte aanbouw in m


@dataclass
class VerbouwingsInfo:
    """Alles wat Sectie 10 toont (Fase 1 MVP)."""
    perceel: Optional[Perceel] = None
    # Pand-footprint VAN DIT PERCEEL — bij rijtjeshuizen/appartementen is de
    # BAG-pand-polygoon groter dan het perceel (één BAG-pand = heel rijtje).
    # We clippen op perceel zodat het getal klopt met wat de koper bezit.
    pand_op_perceel_m2: Optional[int] = None
    # Totale BAG-pand-footprint — alleen informatief (voor rijtjes nuttig).
    pand_totaal_m2: Optional[int] = None
    achtererf: Optional[AchtererfAnalyse] = None
    beschermd_gezicht: Optional[BeschermdGezicht] = None
    gem_monument: Optional[gemeentelijk_monument.GemMonument] = None
    # DSO-data: omgevingsplan-naam + activiteiten (voor Optopping-card
    # en later Vergunningcheck-integratie). None bij ontbrekende DSO-key.
    omgevingsdata: Optional[dso.DSOOmgevingsData] = None
    # Vergunningcheck-resultaten per card (uitbouw, dakkapel, tuinhuis, optopping)
    vergunningcheck_per_card: dict[str, vergunningcheck.VCResultaat] = field(default_factory=dict)
    # 3D BAG pand-hoogte (voor Optopping-card: huidige hoogte t.o.v. BP-max)
    pand_hoogte: Optional[bag3d.PandHoogte] = None
    # BP-regels via Haiku-extractie uit DSO regelteksten
    bp_regels: Optional[bp_extractor.BPRegels] = None
    # BAG-geregistreerde bijgebouwen op hetzelfde perceel (schuren, aanbouwen)
    bijgebouwen: list[bijgebouwen.Bijgebouw] = field(default_factory=list)
    # Wkpb publiekrechtelijke beperkingen (rijks+gemeentelijk monument etc)
    wkpb_beperkingen: list[wkpb.WkpbBeperking] = field(default_factory=list)
    # Stapeling-analyse uit BAG-VBO's van hetzelfde pand
    stapeling: Optional[bag_vbos.PandStapelingInfo] = None
    # Pand-polygoon (intersect met perceel) — gebruikt door zonnepanelen-card
    # voor footprint + oriëntatie-bepaling. Niet geserialiseerd; alleen
    # intern doorgegeven aan orchestrator._build_mogelijkheden.
    pand_op_perceel_poly: Optional[Polygon] = None
    # Woningtype-hint uit perceel/pand-verhouding: 'grondgebonden', 'rij',
    # 'appartement', 'onbekend'. Driveert de UI-disclaimers.
    woning_type_hint: str = "onbekend"
    # Convenience deeplinks (geen API-calls; lokaal samenstellen)
    ruimtelijkeplannen_url: Optional[str] = None
    omgevingsloket_url: Optional[str] = None


# ---------------------------------------------------------------------------
# 1. BRK-perceel
# ---------------------------------------------------------------------------

async def _fetch_perceel(
    client: httpx.AsyncClient, rd_x: float, rd_y: float
) -> Optional[Perceel]:
    """Haal het perceel dat de coord bevat.

    Kleine bbox (3 m) rond het punt; als het punt op een perceel-rand valt
    krijgen we meerdere kandidaten — dan kiezen we het kleinste (meestal de
    eigenlijke kavel en niet een omliggende straat/plein).
    """
    if not (rd_x and rd_y):
        return None
    half = 3
    bbox = f"{rd_x - half},{rd_y - half},{rd_x + half},{rd_y + half},EPSG:28992"
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": "kadastralekaart:Perceel",
        "bbox": bbox,
        "count": "5",
        "outputFormat": "application/json",
        "srsName": "EPSG:28992",
    }
    try:
        resp = await client.get(BRK_WFS, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    feats = data.get("features") or []
    if not feats:
        return None
    # Kies de feature die het punt bevat; fallback: kleinste oppervlakte.
    punt = Point(rd_x, rd_y)
    best = None
    best_area = None
    for f in feats:
        try:
            geom = shape(f.get("geometry") or {})
        except Exception:
            continue
        props = f.get("properties") or {}
        area = props.get("kadastraleGrootteWaarde")
        if geom.contains(punt) or geom.covers(punt):
            # exacte hit wint altijd
            return _perceel_from_feature(f, geom)
        if area is not None and (best_area is None or area < best_area):
            best_area = area
            best = f
    if best is None:
        return None
    try:
        geom = shape(best.get("geometry") or {})
    except Exception:
        geom = None
    return _perceel_from_feature(best, geom)


def _perceel_from_feature(f: dict, geom) -> Perceel:
    p = f.get("properties") or {}
    try:
        nr = int(p.get("perceelnummer") or 0)
    except (TypeError, ValueError):
        nr = 0
    try:
        opp = int(float(p.get("kadastraleGrootteWaarde") or 0))
    except (TypeError, ValueError):
        opp = 0
    # Alleen (Multi)Polygon aanvaarden
    poly = None
    if isinstance(geom, Polygon):
        poly = geom
    elif isinstance(geom, MultiPolygon) and geom.geoms:
        # Voor de analyse pakken we de grootste deel-polygoon — meestal is
        # het perceel één stuk; als er meerdere zijn nemen we de dominante.
        poly = max(geom.geoms, key=lambda g: g.area)
    return Perceel(
        perceelnummer=nr,
        gemeente_code=str(p.get("kadastraleGemeenteCode") or ""),
        oppervlakte_m2=opp,
        polygon_rd=poly,
    )


# ---------------------------------------------------------------------------
# 2. RCE Beschermd gezicht
# ---------------------------------------------------------------------------

async def _fetch_beschermd_gezicht(
    client: httpx.AsyncClient, rd_x: float, rd_y: float
) -> Optional[BeschermdGezicht]:
    """Punt-in-polygon query op rce:Townscapes.

    We sturen een bbox van 1 m rond het punt; de RCE-data is gebied-niveau
    (gezichten zijn tientallen tot honderden hectares) dus de resolutie is
    ruim genoeg.
    """
    if not (rd_x and rd_y):
        return None
    half = 1
    bbox = f"{rd_x - half},{rd_y - half},{rd_x + half},{rd_y + half},EPSG:28992"
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": "rce:Townscapes",
        "bbox": bbox,
        "count": "3",
        "outputFormat": "application/json",
        "srsName": "EPSG:28992",
    }
    try:
        resp = await client.get(RCE_WFS, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    feats = data.get("features") or []
    if not feats:
        return None
    p = feats[0].get("properties") or {}
    naam = p.get("NAAM") or ""
    status = p.get("JURSTATUS") or ""
    if not naam:
        return None
    return BeschermdGezicht(
        naam=naam,
        status=status,
        aangewezen=p.get("AANGEWEZEN"),
    )


# ---------------------------------------------------------------------------
# 3. BAG pand-polygoon in RD
# ---------------------------------------------------------------------------

async def _fetch_pand_polygon_rd(
    client: httpx.AsyncClient, pand_id: str
) -> Optional[Polygon]:
    """Haal de pand-geometrie in RD (meters) voor footprint-berekening.

    BAG-WFS levert standaard in RD als je geen srsName opgeeft — maar we
    zijn expliciet. Key tussen de BAG-WFS (bag:pand) en de BRK-WFS (percelen)
    is dat ze allebei RD-coord gebruiken, wat area-berekening in m² triviaal
    maakt.
    """
    if not pand_id:
        return None
    ogc_filter = (
        "<Filter>"
        "<PropertyIsEqualTo>"
        "<PropertyName>identificatie</PropertyName>"
        f"<Literal>{pand_id}</Literal>"
        "</PropertyIsEqualTo>"
        "</Filter>"
    )
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": "bag:pand",
        "outputFormat": "application/json",
        "srsName": "EPSG:28992",
        "filter": ogc_filter,
    }
    try:
        resp = await client.get(BAG_WFS, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    feats = data.get("features") or []
    if not feats:
        return None
    try:
        geom = shape(feats[0].get("geometry") or {})
    except Exception:
        return None
    if isinstance(geom, Polygon):
        return geom
    if isinstance(geom, MultiPolygon) and geom.geoms:
        return max(geom.geoms, key=lambda g: g.area)
    return None


# ---------------------------------------------------------------------------
# 4. Achtererf-analyse (Shapely)
# ---------------------------------------------------------------------------

def _analyze_achtererf(
    perceel_poly: Polygon, pand_poly: Polygon, entry_rd: Optional[tuple[float, float]]
) -> Optional[AchtererfAnalyse]:
    """Bepaal onbebouwd + achtererf-fractie + max uitbouw-diepte.

    BAG-pand = het hele 'gebouw', wat bij rijtjeshuizen het hele rijtje
    omvat (één pand over 4-8 percelen). Daarom eerst INTERSECTEREN met het
    perceel om het deel op dit specifieke perceel te krijgen; daarna
    percelen − dat deel = onbebouwd terrein op dit perceel.

    Heuristiek voor 'achter':
    - Adres-entry (BAG-entrypoint, meestal voordeur) ligt aan de voorzijde.
    - We splitsen het onbebouwd terrein op de entry→centroid-as: wat achter
      de pand-centroid ligt = achtererf.
    - Max uitbouw-diepte = afstand van pand-achtergevel tot perceel-achter-
      grens, min 1 m burenrecht-marge.

    Bij afwezige entry (of entry binnen het pand): we nemen gewoon het
    totale onbebouwd en claimen geen voor/achter-split; achtererf_m2 wordt
    dan onbebouwd_m2 (hele onbebouwd als achter behandeld, disclaimer in UI).
    """
    try:
        # Pand-footprint beperkt tot dit perceel (belangrijk voor rijtjeshuizen
        # en gestapelde gebouwen waar de BAG-pand-polygoon veel groter is dan
        # het perceel van dit specifieke adres).
        pand_op_perceel = perceel_poly.intersection(pand_poly)
        onbebouwd = perceel_poly.difference(pand_op_perceel)
    except Exception:
        return None
    if onbebouwd.is_empty:
        return AchtererfAnalyse(
            onbebouwd_m2=0,
            onbebouwd_pct=0.0,
            achtererf_m2=0,
            uitbouw_diepte_max_m=0.0,
        )
    onbebouwd_m2 = int(onbebouwd.area)
    perceel_m2 = max(1.0, perceel_poly.area)
    onbebouwd_pct = round(100 * onbebouwd_m2 / perceel_m2, 1)

    # Voor/achter-detectie via een lijn door pand-centroid loodrecht op
    # entry→centroid vector.
    pand_c = pand_poly.centroid
    if entry_rd is None:
        return AchtererfAnalyse(
            onbebouwd_m2=onbebouwd_m2,
            onbebouwd_pct=onbebouwd_pct,
            achtererf_m2=onbebouwd_m2,  # conservatief: hele onbebouwd
            uitbouw_diepte_max_m=None,
        )
    ex, ey = entry_rd
    vx = pand_c.x - ex
    vy = pand_c.y - ey
    norm = math.hypot(vx, vy)
    if norm < 0.5:
        # Entrypoint bijna = centroid → pand zonder heldere voorzijde
        return AchtererfAnalyse(
            onbebouwd_m2=onbebouwd_m2,
            onbebouwd_pct=onbebouwd_pct,
            achtererf_m2=onbebouwd_m2,
            uitbouw_diepte_max_m=None,
        )
    # Eenheidsvector van entry naar centroid (richting voor→achter)
    ux, uy = vx / norm, vy / norm

    # "Achter" = punten waarvoor (pt − centroid) · u > 0
    def _is_back(pt) -> bool:
        return (pt.x - pand_c.x) * ux + (pt.y - pand_c.y) * uy > 0

    # Verzamel alle deel-polygonen in onbebouwd, filter op 'achter'
    parts = list(onbebouwd.geoms) if onbebouwd.geom_type == "MultiPolygon" else [onbebouwd]
    achter_parts = []
    for part in parts:
        # Een deel is 'achter' als zijn centroid aan de achterzijde zit.
        # Grof maar robuust: een smal achtertuin-strook zal altijd aan één
        # kant liggen, niet half-half.
        if _is_back(part.centroid):
            achter_parts.append(part)
    if not achter_parts:
        # Geen enkele strook zit volledig achter — zeldzaam maar mogelijk bij
        # een hoekpand met enkel zij-erf. Val terug op totaal.
        return AchtererfAnalyse(
            onbebouwd_m2=onbebouwd_m2,
            onbebouwd_pct=onbebouwd_pct,
            achtererf_m2=onbebouwd_m2,
            uitbouw_diepte_max_m=None,
        )
    achter = unary_union(achter_parts)
    achter_m2 = int(achter.area)

    # Max uitbouw-diepte: projecteer achter-polygon op de u-as, neem de
    # max afstand voorbij de pand-achtergevel (d.w.z. de max 'diepte').
    # Voor de pand-achtergevel berekenen we de max projectie van pand_poly
    # op u (ten opzichte van centroid); voor achter idem — verschil =
    # hoeveel ruimte er is voorbij de achtergevel.
    def _max_proj(geom) -> float:
        # Verzamel coord-punten en projecteer op u-as t.o.v. centroid
        coords = []
        if geom.geom_type == "Polygon":
            coords = list(geom.exterior.coords)
        elif geom.geom_type == "MultiPolygon":
            for g in geom.geoms:
                coords.extend(list(g.exterior.coords))
        projs = [(c[0] - pand_c.x) * ux + (c[1] - pand_c.y) * uy for c in coords]
        return max(projs) if projs else 0.0

    pand_max = _max_proj(pand_poly)
    achter_max = _max_proj(achter)
    diepte = achter_max - pand_max
    # Alleen zinnig als er > 0 m ruimte achter de gevel is; aftrek 1 m marge.
    if diepte <= ERFGRENS_MARGE_M:
        diepte_m = 0.0
    else:
        diepte_m = round(diepte - ERFGRENS_MARGE_M, 1)

    return AchtererfAnalyse(
        onbebouwd_m2=onbebouwd_m2,
        onbebouwd_pct=onbebouwd_pct,
        achtererf_m2=achter_m2,
        uitbouw_diepte_max_m=diepte_m,
    )


# ---------------------------------------------------------------------------
# Orchestrator entry-point: alles parallel
# ---------------------------------------------------------------------------

async def fetch_verbouwing(
    lat: float, lon: float,
    rd_x: float, rd_y: float,
    bag_pand_id: Optional[str],
    gemeentecode: Optional[str] = None,
    gemeente_naam: Optional[str] = None,
    entry_rd_x: Optional[float] = None,
    entry_rd_y: Optional[float] = None,
    eigen_vbo_id: Optional[str] = None,
) -> VerbouwingsInfo:
    """Haal alle bronnen parallel op en bereken de achtererf-analyse.

    `entry_rd_x/y` = RD-coord van het adres-entrypoint (voordeur), indien
    afwijkend van rd_x/y. De Locatieserver levert `rd_x/y` typisch op de
    ingang, dus als default gebruiken we die ook als entrypoint.
    """
    entry = (
        (entry_rd_x, entry_rd_y)
        if entry_rd_x is not None and entry_rd_y is not None
        else (rd_x, rd_y) if (rd_x and rd_y) else None
    )
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=HEADERS) as client:
        perceel_task = _fetch_perceel(client, rd_x, rd_y)
        gezicht_task = _fetch_beschermd_gezicht(client, rd_x, rd_y)
        pand_task = _fetch_pand_polygon_rd(client, bag_pand_id or "")
        # Gemeentelijk monument + DSO omgevingsdata hebben eigen clients
        # (verschillende hosts), dus we roepen ze parallel met bovenstaande aan.
        gem_task = gemeentelijk_monument.fetch_gemeentelijk_monument(
            gemeentecode=gemeentecode,
            bag_pand_id=bag_pand_id,
            gemeente_naam=gemeente_naam,
        )
        dso_task = dso.fetch_omgevingsdata(rd_x, rd_y)
        vc_task = vergunningcheck.check_alle_werkzaamheden(rd_x, rd_y)
        hoogte_task = bag3d.fetch_pand_hoogte(rd_x, rd_y, bag_pand_id)
        bp_tekst_task = dso.fetch_bp_regeltekst_voor_locatie(rd_x, rd_y)
        wkpb_task = wkpb.fetch_wkpb_monumenten(rd_x, rd_y)
        stapeling_task = bag_vbos.fetch_pand_stapeling(bag_pand_id or "", eigen_vbo_id)
        perceel, gezicht, pand_poly, gem_mon, omgvd, vc, pand_h, bp_t, wkpb_list, stapeling = await asyncio.gather(
            perceel_task, gezicht_task, pand_task, gem_task,
            dso_task, vc_task, hoogte_task, bp_tekst_task, wkpb_task, stapeling_task,
        )
    # Haiku-extractie buiten de DSO-client: eigen Anthropic-SDK call.
    bp_reg: Optional[bp_extractor.BPRegels] = None
    if bp_t is not None:
        _regeling_naam, regeltekst = bp_t
        # Haiku loopt via blocking SDK; voer uit in threadpool zodat
        # event loop niet blokkeert.
        loop = asyncio.get_event_loop()
        try:
            bp_reg = await loop.run_in_executor(
                None, bp_extractor.extract_bp_regels, regeltekst
            )
        except Exception:
            bp_reg = None

    achtererf = None
    pand_totaal_m2 = None
    pand_op_perceel_m2 = None
    pand_op_perceel_poly: Optional[Polygon] = None
    woning_type_hint = "onbekend"
    if pand_poly is not None:
        pand_totaal_m2 = int(pand_poly.area)
    bijgebouwen_list: list[bijgebouwen.Bijgebouw] = []
    if perceel is not None and perceel.polygon_rd is not None and pand_poly is not None:
        achtererf = _analyze_achtererf(perceel.polygon_rd, pand_poly, entry)
        try:
            pand_op_perceel_poly_geom = perceel.polygon_rd.intersection(pand_poly)
            # Intersect kan MultiPolygon worden bij rare percelen — pak grootste
            from shapely.geometry import MultiPolygon as _MP
            if isinstance(pand_op_perceel_poly_geom, _MP) and pand_op_perceel_poly_geom.geoms:
                pand_op_perceel_poly = max(pand_op_perceel_poly_geom.geoms, key=lambda g: g.area)
            elif isinstance(pand_op_perceel_poly_geom, Polygon):
                pand_op_perceel_poly = pand_op_perceel_poly_geom
            pand_op_perceel_m2 = int(pand_op_perceel_poly_geom.area)
        except Exception:
            pand_op_perceel_m2 = None
            pand_op_perceel_poly = None
        # Bijgebouwen-detectie: andere BAG-panden op hetzelfde perceel. Niet
        # blocking — faalt het dan gewoon lege lijst.
        try:
            bijgebouwen_list = await bijgebouwen.fetch_bijgebouwen(
                perceel.polygon_rd, bag_pand_id or ""
            )
        except Exception:
            bijgebouwen_list = []
        # Woning-type-heuristiek als fallback — de autoritatieve bron is
        # BAG aantal_verblijfsobjecten (stap.aantal_wonen) in orchestrator,
        # maar deze hint is nuttig als meta-info voor de frontend/logging.
        if pand_totaal_m2 and perceel.oppervlakte_m2:
            ratio = pand_totaal_m2 / max(1, perceel.oppervlakte_m2)
            if ratio > 3:
                # BAG-pand veel groter dan perceel = rijtjes of appartementen
                woning_type_hint = "rij_of_appartement"
            elif pand_op_perceel_m2 and pand_op_perceel_m2 < pand_totaal_m2 * 0.7:
                # Pand deelt grens met buren → rijtjeshuis / 2-onder-1-kap
                woning_type_hint = "rij"
            else:
                woning_type_hint = "grondgebonden"
    elif pand_poly is not None:
        # Geen perceel maar wel pand-polygoon: gebruik de pand-footprint
        # zelf voor zonnepanelen-berekening.
        pand_op_perceel_poly = pand_poly

    # Deeplinks — beide naar de landing pages van Omgevingsloket. Adres-deeplink
    # werkt niet betrouwbaar in deze SPA's; user voert adres zelf in.
    # URL zoals in het officiële menu (Home → Regels op de kaart)
    rp_url = "https://omgevingswet.overheid.nl/regels-op-de-kaart"
    ol_url = "https://omgevingswet.overheid.nl/checken/nieuw/stap/1"

    return VerbouwingsInfo(
        perceel=perceel,
        pand_op_perceel_m2=pand_op_perceel_m2,
        pand_totaal_m2=pand_totaal_m2,
        achtererf=achtererf,
        beschermd_gezicht=gezicht,
        gem_monument=gem_mon,
        omgevingsdata=omgvd,
        vergunningcheck_per_card=vc or {},
        pand_hoogte=pand_h,
        bp_regels=bp_reg,
        bijgebouwen=bijgebouwen_list,
        wkpb_beperkingen=wkpb_list or [],
        stapeling=stapeling,
        pand_op_perceel_poly=pand_op_perceel_poly,
        woning_type_hint=woning_type_hint,
        ruimtelijkeplannen_url=rp_url,
        omgevingsloket_url=ol_url,
    )
