from common import apply, connected, parser

from lab_control.domain.instruments import AcquisitionRequest
from lab_control.storage.touchstone import export_touchstone


def main():
    args = parser(
        "ZNB3004 sequential S parameter captures and complete two-port Touchstone export"
    ).parse_args()
    with connected(args) as (app, device_id):
        if args.capture:
            traces, folders = {}, []
            for name in ("S11", "S21", "S12", "S22"):
                apply(app, device_id, args, "s_parameter", {"value": name})
                batch = app.sessions.submit(
                    device_id,
                    "acquire",
                    AcquisitionRequest("s_parameters", initiate=True, confirmed=args.confirm),
                ).result()
                folders.append(app.repository.export(batch))
                traces[name] = batch.measurements[0]
            path = export_touchstone(folders[-1] / "network.s2p", traces)
            print(path)
            print("Sequential sweeps: retain every snapshot and verify the DUT remained stable.")


if __name__ == "__main__":
    main()
