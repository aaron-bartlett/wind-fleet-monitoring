"""Synthesize an expanded turbine roster and telemetry history for the Wind Fleet Monitor.

Standalone data-preparation utility. It is **not** part of the application: nothing under
``src/`` imports it, and it is never executed at runtime. Run it once to grow the seed
dataset from 2 turbines to 50, then let the app ingest the result normally.

Inputs (read-only):
    data/farms.csv                  authoritative farm list; never written
    data/ARCHIVE/telemetry.csv      pristine seed telemetry for TURB001/TURB002
    data/ARCHIVE/turbines.csv       pristine seed roster

Outputs (overwritten in place):
    data/turbines.csv               50 rows: the 2 seed turbines + 48 generated
    data/telemetry.csv              seed rows verbatim + generated rows for the 48

The generator is deterministic for a given ``--seed``, so re-running reproduces the same
fleet byte-for-byte. It regenerates from the ARCHIVE originals every time rather than
appending, so repeated runs are idempotent instead of compounding.

Usage:
    python scripts/generate_fleet_data.py
    python scripts/generate_fleet_data.py --seed 7 --turbines 48
    python scripts/generate_fleet_data.py --report      # print the health mix and exit
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------------------
# Turbine physics — mirrors config.py so generated data lands where health.py expects it.
# Kept as local constants rather than importing config, so this script stays runnable
# standalone (e.g. before the package is installed) and never drags the app into scope.
# --------------------------------------------------------------------------------------

CUT_IN_MS = 3.0
RATED_MS = 12.0
CUT_OUT_MS = 25.0
RATED_POWER_KW = 3500.0
RATED_ROTOR_RPM = 16.0

INTERVAL_MINUTES = 5
DEFAULT_SEED = 20260101
DEFAULT_NEW_TURBINES = 48

# Fraction of expected intervals dropped entirely, simulating lost telemetry.
MISSING_INTERVAL_RATE = 0.012
# Fraction of records that arrive unusually late (10-25 min) rather than the normal 1-5 min.
LATE_ARRIVAL_RATE = 0.03

# Health mix targeted across the generated turbines. Health is evaluated on each turbine's
# *latest* record, so every non-healthy profile below must persist through the final
# timestamp — a fault that clears before the end would classify as Healthy on the map.
FAULT_MIX: dict[str, float] = {
    "healthy": 0.60,
    "gearbox_warm": 0.10,  # WARNING  - gearbox drifts into 95-110 C
    "pitch_drift": 0.06,  # WARNING  - blade pitch parks above 25 deg under load
    "gearbox_hot": 0.08,  # CRITICAL - gearbox above 110 C
    "pitch_fault": 0.04,  # CRITICAL - blade pitch stuck above 40 deg
    "no_power": 0.04,  # CRITICAL - zero output in good wind (rotor also stalled)
    "sensor_fault": 0.04,  # ERROR    - physically impossible gearbox reading
    "offline": 0.04,  # ERROR    - stops reporting partway through, latest record stale
}


@dataclass(frozen=True, slots=True)
class Farm:
    """A wind farm read from farms.csv."""

    farm_id: str
    farm_name: str
    latitude: float
    longitude: float


@dataclass(frozen=True, slots=True)
class Turbine:
    """A generated turbine, sited near its parent farm."""

    turbine_id: str
    farm_id: str
    farm_name: str
    latitude: float
    longitude: float
    fault: str
    # Per-unit efficiency multiplier: real fleets have consistently weaker performers.
    efficiency: float


# --------------------------------------------------------------------------------------
# Reading the seed data
# --------------------------------------------------------------------------------------


def read_farms(path: Path) -> list[Farm]:
    """Read farms.csv into Farm records.

    Args:
        path: Path to farms.csv.

    Returns:
        Farms in file order.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If a required column is missing.
    """
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    required = {"farm_id", "farm_name", "latitude", "longitude"}
    if not rows or not required.issubset(rows[0].keys()):
        missing = required.difference(rows[0].keys() if rows else set())
        raise ValueError(f"{path} is missing required column(s): {sorted(missing)}")
    return [
        Farm(
            farm_id=row["farm_id"],
            farm_name=row["farm_name"],
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
        )
        for row in rows
    ]


def read_seed_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read the pristine seed telemetry so it can be re-emitted verbatim.

    Args:
        path: Path to the ARCHIVE telemetry.csv.

    Returns:
        A (fieldnames, rows) pair.
    """
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        return fieldnames, list(reader)


# --------------------------------------------------------------------------------------
# Roster generation
# --------------------------------------------------------------------------------------


def assign_farms(farms: list[Farm], count: int, rng: np.random.Generator) -> list[Farm]:
    """Distribute `count` turbines across farms following a normal distribution.

    Farm index is drawn from N(mu, sigma) centered on the middle of the farm list and
    clipped to its bounds, so mid-list farms host the most turbines and the ends the
    fewest — a bell-shaped fleet rather than an even split.

    Args:
        farms: Farms to draw from.
        count: Number of turbines to assign.
        rng: Seeded random generator.

    Returns:
        One Farm per turbine, in assignment order.
    """
    n = len(farms)
    mu = (n - 1) / 2.0
    sigma = n / 4.0
    draws = rng.normal(mu, sigma, size=count)
    indices = np.clip(np.rint(draws), 0, n - 1).astype(int)
    return [farms[i] for i in indices]


def assign_faults(count: int, rng: np.random.Generator) -> list[str]:
    """Allocate fault profiles to turbines in the proportions given by FAULT_MIX.

    Allocation is by quota rather than per-turbine sampling, so the resulting health mix
    is exact and reproducible instead of drifting with the seed.

    Args:
        count: Number of turbines.
        rng: Seeded random generator, used only to shuffle the assignment.

    Returns:
        One fault-profile name per turbine, shuffled.
    """
    profiles: list[str] = []
    for name, share in FAULT_MIX.items():
        profiles.extend([name] * round(share * count))
    while len(profiles) < count:
        profiles.append("healthy")
    profiles = profiles[:count]
    rng.shuffle(profiles)
    return profiles


def build_turbines(
    farms: list[Farm], count: int, start_index: int, rng: np.random.Generator
) -> list[Turbine]:
    """Generate turbine records sited around their assigned farms.

    Turbines are placed on a jittered ring around the farm coordinate (roughly 0.5-2 km
    out) so they read as a plausible array rather than a single stacked point.

    Args:
        farms: Farms available for assignment.
        count: Number of turbines to generate.
        start_index: First numeric suffix to use for turbine IDs.
        rng: Seeded random generator.

    Returns:
        Generated turbines, ordered by turbine_id.
    """
    assigned = assign_farms(farms, count, rng)
    faults = assign_faults(count, rng)
    per_farm_seen: Counter[str] = Counter()
    turbines: list[Turbine] = []

    for offset, (farm, fault) in enumerate(zip(assigned, faults, strict=True)):
        position = per_farm_seen[farm.farm_id]
        per_farm_seen[farm.farm_id] += 1

        # Golden-angle spiral keeps same-farm turbines from colliding.
        angle = position * 2.399963 + rng.uniform(-0.15, 0.15)
        radius_deg = 0.006 + 0.004 * math.sqrt(position + 1) + rng.uniform(-0.001, 0.001)
        lat = farm.latitude + radius_deg * math.cos(angle)
        # Longitude degrees shrink with latitude; correct so spacing is even on the ground.
        lon = farm.longitude + radius_deg * math.sin(angle) / max(
            math.cos(math.radians(farm.latitude)), 0.1
        )

        turbines.append(
            Turbine(
                turbine_id=f"TURB{start_index + offset:03d}",
                farm_id=farm.farm_id,
                farm_name=farm.farm_name,
                latitude=round(lat, 4),
                longitude=round(lon, 4),
                fault=fault,
                efficiency=float(rng.normal(0.97, 0.045)),
            )
        )
    return turbines


# --------------------------------------------------------------------------------------
# Weather and telemetry synthesis
# --------------------------------------------------------------------------------------


def farm_wind_series(steps: int, rng: np.random.Generator, base_mean_ms: float) -> np.ndarray:
    """Build a farm-wide wind-speed series with diurnal structure and persistence.

    Turbines at the same farm share this series (plus per-turbine noise), so farm-level
    roll-ups behave like real correlated assets rather than 50 independent random walks.

    Args:
        steps: Number of 5-minute intervals.
        rng: Seeded random generator.
        base_mean_ms: Mean wind speed for this farm.

    Returns:
        Wind speed in m/s per interval, clipped to a plausible range.
    """
    hours = np.arange(steps) * (INTERVAL_MINUTES / 60.0)
    # Wind typically peaks in the afternoon and again overnight at plains sites.
    diurnal = 1.8 * np.sin(2 * np.pi * (hours - 4) / 24.0) + 0.7 * np.sin(2 * np.pi * hours / 12.0)

    # AR(1) gust process: wind is strongly autocorrelated at 5-minute resolution.
    phi = 0.94
    noise = rng.normal(0.0, 1.0, size=steps)
    gust = np.zeros(steps)
    for i in range(1, steps):
        gust[i] = phi * gust[i - 1] + noise[i]
    gust *= 1.6 / max(gust.std(), 1e-9)

    return np.clip(base_mean_ms + diurnal + gust, 0.0, 30.0)


def power_curve_kw(wind_speed_ms: np.ndarray) -> np.ndarray:
    """Reference power curve: zero below cut-in, linear ramp to rated, flat, zero above cut-out.

    Args:
        wind_speed_ms: Wind speeds in m/s.

    Returns:
        Expected power output in kW, elementwise.
    """
    ramp = (wind_speed_ms - CUT_IN_MS) / (RATED_MS - CUT_IN_MS) * RATED_POWER_KW
    power = np.where(wind_speed_ms >= RATED_MS, RATED_POWER_KW, ramp)
    power = np.where(wind_speed_ms < CUT_IN_MS, 0.0, power)
    return np.where(wind_speed_ms > CUT_OUT_MS, 0.0, power)


def synthesize_turbine(
    turbine: Turbine,
    timestamps: list[datetime],
    farm_wind: np.ndarray,
    rng: np.random.Generator,
) -> list[dict[str, object]]:
    """Generate the full telemetry history for one turbine.

    Power follows the reference curve with realistic scatter; rotor speed, blade pitch and
    gearbox temperature are all derived from wind and load so the relationships a reviewer
    would plot (wind vs power, load vs temperature) actually hold. The turbine's fault
    profile is then applied over the tail of the series.

    Args:
        turbine: The turbine to generate for.
        timestamps: Measurement times, ascending, tz-aware UTC.
        farm_wind: The parent farm's wind series, one value per timestamp.
        rng: Seeded random generator.

    Returns:
        Telemetry rows as dicts keyed by CSV column name.
    """
    steps = len(timestamps)

    # Per-turbine wind differs slightly from the farm mean (wake effects, siting).
    wind = np.clip(
        farm_wind * rng.normal(1.0, 0.035) + rng.normal(0.0, 0.35, size=steps), 0.0, 32.0
    )

    expected = power_curve_kw(wind)
    # Scatter is proportional to output plus a small floor, which is what makes the
    # wind-vs-power scatter plot look like a real turbine rather than a clean line.
    scatter = rng.normal(0.0, 1.0, size=steps) * (0.05 * expected + 25.0)
    power = np.clip(expected * turbine.efficiency + scatter, 0.0, RATED_POWER_KW)

    # Rotor speed tracks wind up to rated, then holds while pitch sheds the excess.
    rotor = np.where(
        wind < CUT_IN_MS,
        rng.uniform(0.0, 0.4, size=steps),
        np.minimum(RATED_ROTOR_RPM * (wind / RATED_MS), RATED_ROTOR_RPM),
    )
    rotor = np.clip(rotor + rng.normal(0.0, 0.35, size=steps), 0.0, 19.0)

    # Below rated the blades stay near fine pitch; above rated they feather.
    pitch = np.where(
        wind <= RATED_MS, rng.normal(2.4, 0.9, size=steps), 2.0 + 1.9 * (wind - RATED_MS)
    )
    pitch = np.clip(pitch + rng.normal(0.0, 0.35, size=steps), -1.0, 35.0)

    # Gearbox temperature: ambient baseline plus a load-driven rise, smoothed for thermal mass.
    load_fraction = power / RATED_POWER_KW
    raw_temp = 62.0 + 26.0 * load_fraction + rng.normal(0.0, 1.4, size=steps)
    temp = np.copy(raw_temp)
    for i in range(1, steps):
        temp[i] = 0.85 * temp[i - 1] + 0.15 * raw_temp[i]

    _apply_fault(turbine.fault, steps, wind, power, rotor, pitch, temp, rng)

    # Ingest lag: usually 1-5 minutes, occasionally much later.
    lag = rng.integers(1, 6, size=steps)
    late = rng.random(steps) < LATE_ARRIVAL_RATE
    lag = np.where(late, rng.integers(10, 26, size=steps), lag)

    keep = rng.random(steps) >= MISSING_INTERVAL_RATE
    if turbine.fault == "offline":
        # Reporting stops for good roughly 70% of the way through, leaving the latest
        # record stale enough for health.py to classify the turbine as ERROR.
        keep[int(steps * 0.7) :] = False

    rows: list[dict[str, object]] = []
    for i, moment in enumerate(timestamps):
        if not keep[i]:
            continue
        rows.append(
            {
                "turbine_id": turbine.turbine_id,
                "farm_id": turbine.farm_id,
                "timestamp": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "received_at": (moment + timedelta(minutes=int(lag[i]))).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "power_output_kw": round(float(power[i]), 1),
                "wind_speed_ms": round(float(wind[i]), 1),
                "rotor_rpm": round(float(rotor[i]), 1),
                "blade_pitch_deg": round(float(pitch[i]), 1),
                "gearbox_temp_c": round(float(temp[i]), 1),
            }
        )
    return rows


def _apply_fault(
    fault: str,
    steps: int,
    wind: np.ndarray,
    power: np.ndarray,
    rotor: np.ndarray,
    pitch: np.ndarray,
    temp: np.ndarray,
    rng: np.random.Generator,
) -> None:
    """Overlay a fault signature on the tail of a turbine's series, in place.

    Faults ramp in partway through and persist to the final timestamp, so the condition is
    visible both in the history plots and in the turbine's latest-record health status.

    Args:
        fault: Profile name from FAULT_MIX.
        steps: Series length.
        wind: Wind speed series (read-only here).
        power: Power series, mutated for power-related faults.
        rotor: Rotor speed series, mutated for stall faults.
        pitch: Blade pitch series, mutated for pitch faults.
        temp: Gearbox temperature series, mutated for thermal faults.
        rng: Seeded random generator.
    """
    if fault == "healthy":
        return

    onset = int(steps * rng.uniform(0.45, 0.7))
    ramp = np.linspace(0.0, 1.0, steps - onset)
    tail = slice(onset, steps)

    if fault == "gearbox_warm":
        # Drifts into 96-107 C: one minor breach -> WARNING.
        temp[tail] = temp[tail] + ramp * (rng.uniform(98.0, 105.0) - temp[tail])
        temp[tail] += rng.normal(0.0, 0.8, size=steps - onset)
        np.clip(temp[tail], 96.0, 108.0, out=temp[tail])
    elif fault == "gearbox_hot":
        # Runs past 110 C: one major breach -> CRITICAL.
        temp[tail] = temp[tail] + ramp * (rng.uniform(116.0, 128.0) - temp[tail])
        temp[tail] += rng.normal(0.0, 1.2, size=steps - onset)
        np.clip(temp[tail], 112.0, 140.0, out=temp[tail])
    elif fault == "pitch_drift":
        # Parks between 26 and 34 deg while still producing: minor -> WARNING.
        pitch[tail] = np.clip(pitch[tail] + ramp * 30.0, 26.0, 34.0)
        power[tail] *= 0.85
    elif fault == "pitch_fault":
        # Stuck above 40 deg: major -> CRITICAL. Output collapses accordingly.
        pitch[tail] = np.clip(pitch[tail] + ramp * 48.0, 41.0, 62.0)
        power[tail] *= np.linspace(1.0, 0.25, steps - onset)
    elif fault == "no_power":
        # Rotor stalls and output goes to zero in operating wind: major -> CRITICAL.
        power[tail] = rng.uniform(0.0, 0.4, size=steps - onset)
        rotor[tail] = rng.uniform(0.0, 0.3, size=steps - onset)
        pitch[tail] = rng.uniform(84.0, 89.0, size=steps - onset)
        temp[tail] = np.clip(temp[tail] - ramp * 20.0, 30.0, None)
    elif fault == "sensor_fault":
        # Physically impossible reading -> ERROR, which must outrank any breach.
        spike_from = int(steps * 0.92)
        temp[spike_from:] = rng.uniform(210.0, 260.0, size=steps - spike_from)
    elif fault == "offline":
        # Handled by truncating the series in synthesize_turbine.
        return
    else:  # pragma: no cover - guards against a typo in FAULT_MIX
        raise ValueError(f"Unknown fault profile: {fault}")

    _ = wind  # kept in the signature for future wind-conditional faults


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


def build_timeline(seed_rows: list[dict[str, str]]) -> list[datetime]:
    """Derive the generation window from the seed telemetry so old and new rows align.

    Args:
        seed_rows: Rows read from the ARCHIVE telemetry.

    Returns:
        Ascending tz-aware UTC timestamps at the telemetry interval.
    """
    stamps = [
        datetime.strptime(row["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        for row in seed_rows
    ]
    start, end = min(stamps), max(stamps)
    steps = int((end - start).total_seconds() // (INTERVAL_MINUTES * 60)) + 1
    return [start + timedelta(minutes=INTERVAL_MINUTES * i) for i in range(steps)]


def main() -> None:
    """Generate the expanded roster and telemetry, then write both CSVs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--turbines", type=int, default=DEFAULT_NEW_TURBINES)
    parser.add_argument(
        "--report", action="store_true", help="Print the fleet summary without writing files."
    )
    args = parser.parse_args()

    data_dir: Path = args.data_dir
    archive = data_dir / "ARCHIVE"

    farms = read_farms(data_dir / "farms.csv")
    seed_fields, seed_rows = read_seed_rows(archive / "telemetry.csv")
    with (archive / "turbines.csv").open(newline="", encoding="utf-8") as handle:
        seed_turbine_rows = list(csv.DictReader(handle))

    rng = np.random.default_rng(args.seed)
    timestamps = build_timeline(seed_rows)
    turbines = build_turbines(farms, args.turbines, len(seed_turbine_rows) + 1, rng)

    # One shared wind series per farm, so co-located turbines move together.
    farm_means = {farm.farm_id: float(rng.uniform(7.0, 11.5)) for farm in farms}
    farm_series = {
        farm.farm_id: farm_wind_series(len(timestamps), rng, farm_means[farm.farm_id])
        for farm in farms
    }

    telemetry_rows: list[dict[str, object]] = []
    for turbine in turbines:
        telemetry_rows.extend(
            synthesize_turbine(turbine, timestamps, farm_series[turbine.farm_id], rng)
        )

    _print_summary(farms, turbines, seed_rows, telemetry_rows, timestamps)
    if args.report:
        return

    _write_turbines(data_dir / "turbines.csv", seed_turbine_rows, turbines)
    _write_telemetry(data_dir / "telemetry.csv", seed_fields, seed_rows, telemetry_rows)
    print(f"\nWrote {data_dir / 'turbines.csv'} and {data_dir / 'telemetry.csv'}")


def _write_turbines(path: Path, seed_rows: list[dict[str, str]], turbines: list[Turbine]) -> None:
    """Write turbines.csv: the seed rows verbatim, then the generated ones."""
    fields = ["turbine_id", "farm_id", "farm_name", "latitude", "longitude"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in seed_rows:
            writer.writerow({key: row[key] for key in fields})
        for turbine in turbines:
            writer.writerow(
                {
                    "turbine_id": turbine.turbine_id,
                    "farm_id": turbine.farm_id,
                    "farm_name": turbine.farm_name,
                    "latitude": turbine.latitude,
                    "longitude": turbine.longitude,
                }
            )


def _write_telemetry(
    path: Path,
    fields: list[str],
    seed_rows: list[dict[str, str]],
    generated: list[dict[str, object]],
) -> None:
    """Write telemetry.csv: seed rows verbatim, then generated rows, sorted by id and time."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(seed_rows)
        generated.sort(key=lambda row: (str(row["turbine_id"]), str(row["timestamp"])))
        writer.writerows(generated)


def _print_summary(
    farms: list[Farm],
    turbines: list[Turbine],
    seed_rows: list[dict[str, str]],
    generated: list[dict[str, object]],
    timestamps: list[datetime],
) -> None:
    """Print the farm distribution, fault mix, and row counts."""
    per_farm = Counter(t.farm_id for t in turbines)
    print("Turbines per farm (seed turbines not counted):")
    for farm in farms:
        count = per_farm.get(farm.farm_id, 0)
        print(f"  {farm.farm_id} {farm.farm_name:<16} {count:>2}  {'#' * count}")

    print("\nFault profiles:")
    for name, count in sorted(Counter(t.fault for t in turbines).items()):
        print(f"  {name:<14} {count:>2}")

    print(
        f"\nWindow:    {timestamps[0]:%Y-%m-%d %H:%M} -> {timestamps[-1]:%Y-%m-%d %H:%M} UTC"
        f"  ({len(timestamps)} intervals)"
    )
    print(f"Seed rows:      {len(seed_rows):>7,}")
    print(f"Generated rows: {len(generated):>7,}")
    print(f"Total rows:     {len(seed_rows) + len(generated):>7,}")


if __name__ == "__main__":
    main()
