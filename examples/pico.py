from common import apply, connected, parser

from lab_control.domain.instruments import AcquisitionRequest


def main():
    cli = parser("Pico ps6000a block/streaming example")
    cli.add_argument("--kind", choices=("waveform", "streaming"), default="waveform")
    args = cli.parse_args()
    with connected(args) as (app, device_id):
        if args.capture:
            apply(
                app,
                device_id,
                args,
                "channel",
                {"enabled": True, "range": "1", "coupling": "DC", "offset": 0},
            )
            batch = app.sessions.submit(
                device_id,
                "acquire",
                AcquisitionRequest(args.kind, (args.channel,), 1024, initiate=True, confirmed=args.confirm),
            ).result()
            print(app.repository.export(batch))


if __name__ == "__main__":
    main()
