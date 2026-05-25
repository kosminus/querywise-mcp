"""Build a zero-infrastructure IFRS 9 sample database as a local SQLite file.

The original QueryWise shipped this sample as a PostgreSQL container. Here we
generate an equivalent SQLite file so `querywise seed-sample` works with no
Docker/Postgres. The schema matches every table/column referenced by the baked
-in semantic metadata (glossary, metrics, dictionary) in ``setup_service``.

Data is deterministic (fixed RNG seed) so repeated builds are identical.
"""

import logging
import os
import random
import sqlite3
from datetime import date, timedelta

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE counterparties (
    counterparty_id INTEGER PRIMARY KEY,
    name            TEXT    NOT NULL,
    segment         TEXT    NOT NULL,   -- retail | corporate | sme
    credit_rating   TEXT    NOT NULL,   -- AAA..CCC
    is_defaulted    INTEGER NOT NULL    -- 0 | 1
);

CREATE TABLE facilities (
    facility_id      INTEGER PRIMARY KEY,
    counterparty_id  INTEGER NOT NULL REFERENCES counterparties(counterparty_id),
    -- facility_type: mortgage|corporate_loan|consumer_loan|credit_card|overdraft
    facility_type    TEXT    NOT NULL,
    currency         TEXT    NOT NULL,  -- EUR|USD|GBP
    is_revolving     INTEGER NOT NULL,  -- 0 | 1
    limit_amount     REAL    NOT NULL,
    origination_date TEXT    NOT NULL
);

CREATE TABLE exposures (
    exposure_id     INTEGER PRIMARY KEY,
    facility_id     INTEGER NOT NULL REFERENCES facilities(facility_id),
    reporting_date  TEXT    NOT NULL,
    ead             REAL    NOT NULL,   -- exposure at default
    carrying_amount REAL    NOT NULL,
    stage           INTEGER NOT NULL,   -- 1 | 2 | 3
    days_past_due   INTEGER NOT NULL
);

CREATE TABLE ecl_provisions (
    provision_id INTEGER PRIMARY KEY,
    exposure_id  INTEGER NOT NULL REFERENCES exposures(exposure_id),
    pd           REAL    NOT NULL,      -- probability of default
    lgd          REAL    NOT NULL,      -- loss given default
    ecl_12m      REAL    NOT NULL,
    ecl_lifetime REAL    NOT NULL,
    stage        INTEGER NOT NULL
);

CREATE TABLE collateral (
    collateral_id   INTEGER PRIMARY KEY,
    facility_id     INTEGER NOT NULL REFERENCES facilities(facility_id),
    collateral_type TEXT    NOT NULL,   -- property|cash|guarantee|securities
    value           REAL    NOT NULL
);

CREATE TABLE staging_history (
    history_id     INTEGER PRIMARY KEY,
    facility_id    INTEGER NOT NULL REFERENCES facilities(facility_id),
    from_stage     INTEGER NOT NULL,
    to_stage       INTEGER NOT NULL,
    reason         TEXT    NOT NULL,    -- origination|upgrade|downgrade|cure|default
    effective_date TEXT    NOT NULL
);
"""

_SEGMENTS = ["retail", "corporate", "sme"]
_RATINGS = ["AAA", "AA", "A", "BBB", "BB", "B", "CCC"]
_FACILITY_TYPES = ["mortgage", "corporate_loan", "consumer_loan", "credit_card", "overdraft"]
_REVOLVING_TYPES = {"credit_card", "overdraft"}
_CURRENCIES = ["EUR", "USD", "GBP"]
_COLLATERAL_TYPES = ["property", "cash", "guarantee", "securities"]
_REPORTING_DATE = "2024-12-31"


def _has_data(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        con = sqlite3.connect(path)
        try:
            n = con.execute("SELECT COUNT(*) FROM counterparties").fetchone()[0]
            return n > 0
        finally:
            con.close()
    except sqlite3.Error:
        return False


def build_sample_sqlite(path: str, force: bool = False) -> bool:
    """Create the IFRS 9 sample SQLite file at ``path``.

    Idempotent: returns False without rebuilding if the file already has data
    (unless ``force``). Returns True when it (re)built the database.
    """
    if _has_data(path) and not force:
        return False

    if force and os.path.exists(path):
        os.remove(path)

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    rng = random.Random(42)

    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA_SQL)
        _populate(con, rng)
        con.commit()
    finally:
        con.close()

    logger.info("Built IFRS 9 sample SQLite database at %s", path)
    return True


def _populate(con: sqlite3.Connection, rng: random.Random) -> None:
    base = date(2024, 12, 31)

    # --- counterparties (20) ---
    names = [
        "Atlas Manufacturing", "Bluewave Logistics", "Cedar Retail Group", "Delta Energy",
        "Evergreen Properties", "Ferndale Foods", "Granite Capital", "Harbor Shipping",
        "Ironclad Steel", "Juniper Pharma", "Kestrel Airlines", "Lumen Telecom",
        "Maple Auto", "Northstar Mining", "Orchard Agritech", "Pinnacle Hotels",
        "Quartz Construction", "Riverside Utilities", "Summit Software", "Tidewater Marine",
    ]
    counterparties = []
    for i, nm in enumerate(names, start=1):
        segment = rng.choice(_SEGMENTS)
        rating = rng.choices(_RATINGS, weights=[3, 5, 8, 10, 7, 4, 2])[0]
        # Worse ratings are more likely to be in default.
        default_prob = {"AAA": 0, "AA": 0.0, "A": 0.02, "BBB": 0.05,
                        "BB": 0.15, "B": 0.35, "CCC": 0.6}[rating]
        is_defaulted = 1 if rng.random() < default_prob else 0
        counterparties.append((i, nm, segment, rating, is_defaulted))
    con.executemany(
        "INSERT INTO counterparties VALUES (?, ?, ?, ?, ?)", counterparties
    )

    # --- facilities (~25): 1-2 per counterparty ---
    facilities = []
    fid = 0
    for cp in counterparties:
        cp_id = cp[0]
        for _ in range(rng.randint(1, 2)):
            fid += 1
            ftype = rng.choice(_FACILITY_TYPES)
            revolving = 1 if ftype in _REVOLVING_TYPES else 0
            currency = rng.choices(_CURRENCIES, weights=[6, 3, 2])[0]
            limit_amount = round(rng.uniform(50_000, 5_000_000), 2)
            orig = base - timedelta(days=rng.randint(180, 3650))
            facilities.append(
                (fid, cp_id, ftype, currency, revolving, limit_amount, orig.isoformat())
            )
            if fid >= 25:
                break
        if fid >= 25:
            break
    con.executemany(
        "INSERT INTO facilities VALUES (?, ?, ?, ?, ?, ?, ?)", facilities
    )
    cp_defaulted = {cp[0]: cp[4] for cp in counterparties}

    # --- exposures (one current snapshot per facility) ---
    exposures = []
    for f in facilities:
        f_id, cp_id, ftype = f[0], f[1], f[2]
        limit_amount = f[5]
        defaulted = cp_defaulted[cp_id]
        utilisation = rng.uniform(0.3, 1.0)
        ead = round(limit_amount * utilisation, 2)
        if defaulted:
            stage, dpd = 3, rng.randint(90, 365)
        else:
            roll = rng.random()
            if roll < 0.65:
                stage, dpd = 1, 0
            elif roll < 0.9:
                stage, dpd = 2, rng.randint(31, 89)
            else:
                stage, dpd = 3, rng.randint(90, 200)
        carrying = round(ead * rng.uniform(0.9, 1.0), 2)
        exposures.append((f_id, f_id, _REPORTING_DATE, ead, carrying, stage, dpd))
    con.executemany(
        "INSERT INTO exposures VALUES (?, ?, ?, ?, ?, ?, ?)", exposures
    )

    # --- ecl_provisions (one per exposure) ---
    provisions = []
    for ex in exposures:
        ex_id, ead, stage = ex[0], ex[3], ex[5]
        if stage == 1:
            pd_12m = rng.uniform(0.002, 0.03)
            pd_life = pd_12m
        elif stage == 2:
            pd_12m = rng.uniform(0.05, 0.15)
            pd_life = rng.uniform(0.2, 0.45)
        else:
            pd_12m = rng.uniform(0.4, 0.8)
            pd_life = rng.uniform(0.85, 1.0)
        lgd = round(rng.uniform(0.25, 0.6), 4)
        ecl_12m = round(ead * pd_12m * lgd, 2)
        ecl_lifetime = round(ead * pd_life * lgd, 2)
        if stage == 1:
            ecl_lifetime = round(max(ecl_lifetime, ecl_12m), 2)
        provisions.append(
            (ex_id, ex_id, round(pd_12m, 4), lgd, ecl_12m, ecl_lifetime, stage)
        )
    con.executemany(
        "INSERT INTO ecl_provisions VALUES (?, ?, ?, ?, ?, ?, ?)", provisions
    )

    # --- collateral (~14): mainly on mortgages / corporate loans ---
    collateral = []
    cid = 0
    for f in facilities:
        f_id, ftype = f[0], f[2]
        if ftype == "mortgage":
            ctype, frac = "property", rng.uniform(0.8, 1.3)
        elif ftype == "corporate_loan" and rng.random() < 0.6:
            ctype, frac = rng.choice(["property", "securities", "guarantee"]), rng.uniform(0.4, 0.9)
        elif rng.random() < 0.2:
            ctype, frac = rng.choice(["cash", "securities"]), rng.uniform(0.2, 0.6)
        else:
            continue
        cid += 1
        value = round(f[5] * frac, 2)
        collateral.append((cid, f_id, ctype, value))
    con.executemany(
        "INSERT INTO collateral VALUES (?, ?, ?, ?)", collateral
    )

    # --- staging_history (~30): origination for each facility + transitions ---
    history = []
    hid = 0
    ex_by_fac = {ex[1]: ex for ex in exposures}
    for f in facilities:
        f_id = f[0]
        orig = date.fromisoformat(f[6])
        hid += 1
        history.append((hid, f_id, 1, 1, "origination", orig.isoformat()))
        cur_stage = ex_by_fac[f_id][5]
        if cur_stage != 1:
            transition = base - timedelta(days=rng.randint(30, 400))
            reason = "default" if cur_stage == 3 else "downgrade"
            hid += 1
            history.append(
                (hid, f_id, 1, cur_stage, reason, transition.isoformat())
            )
    con.executemany(
        "INSERT INTO staging_history VALUES (?, ?, ?, ?, ?, ?)", history
    )
