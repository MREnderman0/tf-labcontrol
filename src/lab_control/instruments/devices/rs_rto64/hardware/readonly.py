"""Reserved explicit entry; no hardware access is authorized by package migration."""

from lab_control.domain.instruments import Unsupported


def main() -> int:
    raise Unsupported("本型号只读硬件入口尚未验收；请先按包内硬件计划明确资产及范围")


if __name__ == "__main__":
    raise SystemExit(main())
