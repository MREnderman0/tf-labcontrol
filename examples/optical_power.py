from common import apply, connected, parser

from lab_control.domain.instruments import AcquisitionRequest


def main():
    cli = parser("Set sensor wavelength and read optical power")
    cli.add_argument("--wavelength", type=float, default=1550)
    args = cli.parse_args()
    with connected(args) as (app, device_id):
        if args.write:
            apply(app, device_id, args, "wavelength", {"value": args.wavelength})
        if args.capture:
            print(
                app.repository.export(
                    app.sessions.submit(device_id, "acquire", AcquisitionRequest()).result(), "optical"
                )
            )


if __name__ == "__main__":
    main()
