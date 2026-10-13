# Zuordnung EU-Luftwiderstandsklassen (A1–A24) zu CdxA-Mittelwerten [m²]
# Quelle: EU-Verordnung zur HDV-Zertifizierung (HDV CO2-Zertifizierung)
# Jeweils arithmetisches Mittel der Klassengrenzwerte.
# Ausnahme A1 (0,00–3,00): Mittelwert 1,50 dient als Platzhalter für
# sehr aerodynamische Fahrzeuge ohne engere Klassifizierung.

CDXA_CLASS_MIDPOINTS: dict[str, float] = {
    "A1":  1.500,   # 0,00–3,00
    "A2":  3.075,   # 3,00–3,15
    "A3":  3.230,   # 3,15–3,31
    "A4":  3.395,   # 3,31–3,48
    "A5":  3.565,   # 3,48–3,65
    "A6":  3.740,   # 3,65–3,83
    "A7":  3.925,   # 3,83–4,02
    "A8":  4.120,   # 4,02–4,22
    "A9":  4.325,   # 4,22–4,43
    "A10": 4.540,   # 4,43–4,65
    "A11": 4.765,   # 4,65–4,88
    "A12": 5.000,   # 4,88–5,12
    "A13": 5.250,   # 5,12–5,38
    "A14": 5.515,   # 5,38–5,65
    "A15": 5.790,   # 5,65–5,93
    "A16": 6.080,   # 5,93–6,23
    "A17": 6.385,   # 6,23–6,54
    "A18": 6.705,   # 6,54–6,87
    "A19": 7.040,   # 6,87–7,21
    "A20": 7.390,   # 7,21–7,57
    "A21": 7.760,   # 7,57–7,95
    "A22": 8.150,   # 7,95–8,35
    "A23": 8.560,   # 8,35–8,77
    "A24": 8.990,   # 8,77–9,21
}


def get_cdxa(klasse: str) -> float:
    """Gibt den CdxA-Mittelwert [m²] für eine EU-Luftwiderstandsklasse zurück.

    Args:
        klasse: Klassenbezeichnung, z. B. "A8" oder "a8" (Groß-/Kleinschreibung egal).

    Returns:
        CdxA-Mittelwert in m².

    Raises:
        KeyError: Wenn die Klasse nicht bekannt ist.
    """
    key = klasse.upper()
    if key not in CDXA_CLASS_MIDPOINTS:
        raise KeyError(
            f"Unbekannte CdxA-Klasse '{klasse}'. "
            f"Gültige Klassen: {list(CDXA_CLASS_MIDPOINTS)}"
        )
    return CDXA_CLASS_MIDPOINTS[key]
