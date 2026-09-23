from common import apply, connected, parser


def main():
    cli = parser("Set current limit then voltage; never enables output")
    cli.add_argument("--voltage", type=float, default=1)
    cli.add_argument("--current", type=float, default=0.05)
    args = cli.parse_args()
    with connected(args) as (app, device_id):
        if args.write:
            apply(app, device_id, args, "current", {"value": args.current})
            apply(app, device_id, args, "voltage", {"value": args.voltage})


if __name__ == "__main__":
    main()
