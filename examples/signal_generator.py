import math

from common import apply, connected, parser


def main():
    cli = parser("Signal source settings and optional waveform upload; never enables output")
    cli.add_argument("--frequency", type=float, default=1000)
    cli.add_argument("--upload", action="store_true")
    args = cli.parse_args()
    with connected(args) as (app, device_id):
        if args.write:
            apply(app, device_id, args, "frequency", {"value": args.frequency})
        if args.upload:
            samples = [math.sin(2 * math.pi * i / 1024) for i in range(1024)]
            apply(app, device_id, args, "upload_waveform", {"name": "LabTrial", "samples": samples})


if __name__ == "__main__":
    main()
