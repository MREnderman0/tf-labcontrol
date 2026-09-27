"""Offline DHO924 latency comparison; synthetic SCPI, metadata only, no hardware."""

import argparse
import json
import statistics
import time
from pathlib import Path

import numpy as np

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand
from lab_control.domain.oscilloscope import CONTROL_NAMES
from lab_control.instruments.devices.rigol_dho924.tests.test_adapter import ready


def run(*, delay: float, rounds: int, samples: int) -> dict:
    rows = []
    for round_number in range(rounds):
        for optimized in (False, True):
            adapter, peer = ready()
            request = AcquisitionRequest("waveform", (1, 2, 3, 4), consistency="live", context_id="bench")
            adapter.acquire(request)
            query = peer.query

            def delayed(payload):
                time.sleep(delay)
                return query(payload)

            peer.query = delayed
            for sample in range(samples + 1):  # One warmup per candidate/round.
                peer.history.clear()
                command = DeviceCommand("offset", {"value": (sample % 2) * 0.01})
                start = time.perf_counter()
                result = adapter.apply(command) if optimized else adapter._apply_command(command)
                assert result.outcome == "success"
                applied = time.perf_counter()
                adapter.refresh_capabilities()
                if not optimized:
                    # Previous UI reread every visible field in nine queued batches.
                    keys = sorted(
                        {
                            (ch if adapter.profile.commands[name].capability.channel_scoped else 1, name)
                            for ch in adapter.profile.channels
                            for name in (*CONTROL_NAMES, "record_length", "coupling", "probe_attenuation")
                        }
                    )
                    for ch, name in keys:
                        adapter.read(name, ch)
                refreshed = time.perf_counter()
                batch = adapter.acquire(request)
                finished = time.perf_counter()
                assert all(len(m.values) == 1000 for m in batch.measurements)
                if sample:
                    rows.append(
                        {
                            "round": round_number + 1,
                            "optimized": optimized,
                            "sample": sample,
                            "queries": sum("?" in q for q in peer.history),
                            "control_seconds": applied - start,
                            "refresh_seconds": refreshed - applied,
                            "first_frame_seconds": finished - refreshed,
                            "total_seconds": finished - start,
                            "validation": batch.validation,
                        }
                    )
    groups = []
    for round_number in range(1, rounds + 1):
        comparison = {}
        for optimized in (False, True):
            group = [r for r in rows if r["round"] == round_number and r["optimized"] == optimized]
            seconds = [r["total_seconds"] for r in group]
            comparison["optimized" if optimized else "baseline"] = {
                "median_seconds": statistics.median(seconds),
                "p95_seconds": float(np.percentile(seconds, 95)),
                "max_seconds": max(seconds),
                "queries": sorted({r["queries"] for r in group}),
                **{
                    phase: statistics.median(r[phase] for r in group)
                    for phase in ("control_seconds", "refresh_seconds", "first_frame_seconds")
                },
            }
        baseline, optimized_result = comparison["baseline"], comparison["optimized"]
        reduction = 1 - optimized_result["median_seconds"] / baseline["median_seconds"]
        assert reduction >= 0.3 and optimized_result["p95_seconds"] <= baseline["p95_seconds"]
        groups.append({"round": round_number, "reduction": reduction, **comparison})
    return {
        "mode": "offline_synthetic",
        "delay_per_query_seconds": delay,
        "samples_per_round": samples,
        "scope": "DHO924 four channels; worker path, excludes Qt cadence and native instrument delay",
        "groups": groups,
        "raw": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--delay", type=float, default=0.005)
    parser.add_argument("--samples", type=int, default=10)
    args = parser.parse_args()
    result = run(delay=args.delay, rounds=3, samples=args.samples)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["groups"], indent=2))
