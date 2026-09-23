from common import connected, parser

from lab_control.domain.instruments import AcquisitionRequest


def main():
    cli = parser("Single acquisition or repeated batches through the shared session worker")
    cli.add_argument("--kind", default="scalar")
    cli.add_argument("--quantity", default="")
    cli.add_argument("--points", type=int, default=1000)
    cli.add_argument("--initiate", action="store_true")
    cli.add_argument("--stream", action="store_true")
    cli.add_argument("--count", type=int, default=10)
    cli.add_argument("--mode", choices=("electronic", "optical"), default="electronic")
    args = cli.parse_args()
    with connected(args) as (app, device_id):
        request = AcquisitionRequest(
            args.kind, (args.channel,), args.points, args.quantity, args.initiate, args.confirm
        )
        if args.stream:
            print(
                app.sessions.start_acquisition(device_id, request, count=args.count, mode=args.mode).result()
            )
        elif args.capture:
            batch = app.sessions.submit(device_id, "acquire", request).result()
            print(app.repository.export(batch, args.mode))


if __name__ == "__main__":
    main()
